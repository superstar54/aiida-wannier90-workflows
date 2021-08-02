import typing
import enum
import numpy as np

from aiida import orm
from aiida.common import AttributeDict
from aiida.common.lang import type_check
from aiida.engine.processes import WorkChain, ToContext, if_, ProcessBuilder
from aiida.engine.processes import calcfunction
from aiida.plugins import WorkflowFactory, CalculationFactory, GroupFactory

from aiida_quantumespresso.utils.mapping import prepare_process_inputs
from aiida_quantumespresso.workflows.pw.base import PwBaseWorkChain
from aiida_quantumespresso.workflows.pw.relax import PwRelaxWorkChain
from aiida_quantumespresso.calculations.projwfc import ProjwfcCalculation
from aiida_quantumespresso.calculations.pw2wannier90 import Pw2wannier90Calculation
from aiida_wannier90.calculations import Wannier90Calculation
from aiida_quantumespresso.common.types import ElectronicType, SpinType
from aiida_quantumespresso.workflows.protocols.utils import ProtocolMixin

from aiida_pseudo.data.pseudo import UpfData

from .base import Wannier90BaseWorkChain
from ..calculations.functions.kmesh import get_explicit_kpoints_from_distance, get_explicit_kpoints, create_kpoints_from_distance
from ..utils.scdm import fit_scdm_mu_sigma_aiida, get_energy_of_projectability
from ..utils.upf import get_number_of_projections, get_wannier_number_of_bands, _load_pseudo_metadata

__all__ = ['Wannier90WorkChain']


class WannierProjectionType(enum.Enum):
    """Enumeration to indicate the Wannier initial projection type."""

    HYDROGEN = 'hydrogen'
    NUMERIC = 'numeric'
    SCDM = 'scdm'
    RANDOM = 'random'

class WannierDisentanglementType(enum.Enum):
    """Enumeration to indicate the Wannier disentanglement type."""

    # no disentanglement
    NONE = 'none'
    # a fixed dis_froz_max, default is fermi_energy + 2 eV
    WINDOW_FIXED = 'window_fixed'
    # automatically calculate dis_froz_max based on bands projectability, default projectability threshold is 0.9
    WINDOW_AUTO = 'window_auto'
    # disentaglement per kpoint based on projectability, default thresholds are min/max = 0.01/0.95
    PROJECTABILITY = 'projectability'
    # fixed window + projectability per kpoint, default is fermi_energy + 2 eV and min/max = 0.01/0.95
    WINDOW_AND_PROJECTABILITY = 'window_and_projectability'
    # automatically choose the best disentanglement according to WannierProjectionType and ElectronicType
    # for ElectronicType.INSULATOR, use NONE
    # for metals or insulators with conduction bands:
    #   for WannierProjectionType.HYDROGEN/RANDOM, use WINDOW_FIXED
    #   for WannierProjectionType.NUMERIC, use WINDOW_AND_PROJECTABILITY
    #   for WannierProjectionType.SCDM, use NONE
    AUTO = 'auto'


class Wannier90WorkChain(ProtocolMixin, WorkChain):
    """
    Workchain to obtain maximally localised Wannier functions (MLWF)
    Authors: Antimo Marrazzo (antimo.marrazzo@epfl.ch), Giovanni Pizzi (giovanni.pizzi@epfl.ch), Junfeng Qiao(junfeng.qiao@epfl.ch)
    
    MIT License - Copyright (c), 2018, ECOLE POLYTECHNIQUE FEDERALE DE LAUSANNE
    (Theory and Simulation of Materials (THEOS) and National Centre for 
    Computational Design and Discovery of Novel Materials (NCCR MARVEL)).
    All rights reserved.

    Scheme: setup --> relax(optional) --> scf --> nscf --> projwfc 
            -> wannier90_postproc --> pw2wannier90 --> wannier90 --> results
    
    This is a very basic workchain, in that user needs to specify 
    inputs of every step. Please consider using Wannier90BandsWorkChain, 
    which automatically generates inputs.
    """
    @classmethod
    def define(cls, spec):
        super().define(spec)

        spec.input(
            'structure',
            valid_type=orm.StructureData,
            help='The input structure.'
        )
        spec.input(
            'clean_workdir',
            valid_type=orm.Bool,
            required=False,
            default=lambda: orm.Bool(False),
            help=
            'If `True`, work directories of all called calculation will be cleaned at the end of execution.'
        )
        spec.input(
            'relative_dis_windows',
            valid_type=orm.Bool,
            required=False,
            default=lambda: orm.Bool(False),
            help=
            'If True the dis_froz/win_min/max will be shifted by fermi_enerngy. False is the default behaviour of wannier90.'
        )
        spec.input(
            'auto_froz_max',
            valid_type=orm.Bool,
            required=False,
            default=lambda: orm.Bool(False),
            help=
            'If True use the energy corresponding to projectability = 0.9 as dis_froz_max for wannier90 disentanglement.'
        )
        spec.input(
            'auto_froz_max_threshold',
            valid_type=orm.Float,
            required=False,
            default=lambda: orm.Float(0.9),
            help=
            'Threshold for auto_froz_max.'
        )
        spec.expose_inputs(
            PwRelaxWorkChain,
            namespace='relax',
            exclude=('clean_workdir', 'structure'),
            namespace_options={
                'required': False,
                'populate_defaults': False,
                'help':
                'Inputs for the `PwRelaxWorkChain`, if not specified at all, the relaxation step is skipped.'
            }
        )
        spec.expose_inputs(
            PwBaseWorkChain,
            namespace='scf',
            exclude=('clean_workdir', 'pw.structure'),
            namespace_options={
                'help':
                'Inputs for the `PwBaseWorkChain` for the SCF calculation.'
            }
        )
        spec.expose_inputs(
            PwBaseWorkChain,
            namespace='nscf',
            exclude=('clean_workdir', 'pw.structure'),
            namespace_options={
                'required':
                False,
                'populate_defaults':
                False,
                'help':
                'Inputs for the `PwBaseWorkChain` for the NSCF calculation.'
            }
        )
        spec.expose_inputs(
            ProjwfcCalculation,
            namespace='projwfc',
            exclude=('parent_folder', ),
            namespace_options={
                'required':
                False,
                'help':
                'Inputs for the `ProjwfcCalculation` for the Projwfc calculation.'
            }
        )
        spec.expose_inputs(
            Pw2wannier90Calculation,
            namespace='pw2wannier90',
            exclude=('parent_folder', 'nnkp_file'),
            namespace_options={
                'help':
                'Inputs for the `Pw2wannier90Calculation` for the pw2wannier90 calculation.'
            }
        )
        spec.expose_inputs(
            Wannier90Calculation,
            namespace='wannier90',
            exclude=('structure', ),
            namespace_options={
                'help':
                'Inputs for the `Wannier90Calculation` for the Wannier90 calculation.'
            }
        )

        spec.outline(
            cls.setup,
            cls.validate_parameters,
            if_(cls.should_run_relax)(
                cls.run_relax,
                cls.inspect_relax,
            ),
            if_(cls.should_run_scf)(
                cls.run_scf,
                cls.inspect_scf,
            ),
            if_(cls.should_run_nscf)(
                cls.run_nscf,
                cls.inspect_nscf,
            ),
            if_(cls.should_run_projwfc)(
                cls.run_projwfc,
                cls.inspect_projwfc,
            ),
            cls.run_wannier90_pp,
            cls.inspect_wannier90_pp,
            cls.run_pw2wannier90,
            cls.inspect_pw2wannier90,
            cls.run_wannier90,
            cls.inspect_wannier90,
            cls.results,
        )

        spec.expose_outputs(
            PwRelaxWorkChain,
            namespace='relax',
            namespace_options={'required': False}
        )
        spec.expose_outputs(
            PwBaseWorkChain,
            namespace='scf',
            namespace_options={'required': False}
        )
        spec.expose_outputs(
            PwBaseWorkChain,
            namespace='nscf',
            namespace_options={'required': False}
        )
        spec.expose_outputs(
            ProjwfcCalculation,
            namespace='projwfc',
            namespace_options={'required': False}
        )
        spec.expose_outputs(Pw2wannier90Calculation, namespace='pw2wannier90')
        spec.expose_outputs(Wannier90BaseWorkChain, namespace='wannier90_pp')
        spec.expose_outputs(Wannier90Calculation, namespace='wannier90')

        spec.exit_code(
            401,
            'ERROR_INVALID_INPUT_KPOINT_PATH',
            message='bands_plot is True but no kpoint_path provided'
        )
        spec.exit_code(
            402,
            'ERROR_INVALID_INPUT_RELATIVE_DIS_WINDOWS',
            message='relative_dis_windows is True but no fermi_energy provided'
        )
        spec.exit_code(
            403,
            'ERROR_INVALID_INPUT_PSEUDOPOTENTIAL',
            message='Invalid pseudopotentials.'
        )
        spec.exit_code(
            410,
            'ERROR_SUB_PROCESS_FAILED_RELAX',
            message='the PwRelaxWorkChain sub process failed'
        )
        spec.exit_code(
            420,
            'ERROR_SUB_PROCESS_FAILED_SCF',
            message='the scf PwBasexWorkChain sub process failed'
        )
        spec.exit_code(
            430,
            'ERROR_SUB_PROCESS_FAILED_NSCF',
            message='the nscf PwBasexWorkChain sub process failed'
        )
        spec.exit_code(
            440,
            'ERROR_SUB_PROCESS_FAILED_PROJWFC',
            message='the ProjwfcCalculation sub process failed'
        )
        spec.exit_code(
            450,
            'ERROR_SUB_PROCESS_FAILED_WANNIER90PP',
            message='the postproc Wannier90Calculation sub process failed'
        )
        spec.exit_code(
            460,
            'ERROR_SUB_PROCESS_FAILED_PW2WANNIER90',
            message='the Pw2wannier90Calculation sub process failed'
        )
        spec.exit_code(
            470,
            'ERROR_SUB_PROCESS_FAILED_WANNIER90',
            message='the Wannier90Calculation sub process failed'
        )

    def setup(self):
        """Define the current structure in the context to be the input structure."""
        self.ctx.current_structure = self.inputs.structure

    def validate_parameters(self):
        """Validate the input parameters."""
        wannier_inputs = AttributeDict(self.inputs['wannier90'])
        parameters = wannier_inputs.parameters.get_dict()

        # Check bands_plot and kpoint_path
        bands_plot = parameters.get('bands_plot', False)
        if bands_plot:
            kpoint_path = wannier_inputs.get('kpoint_path', None)
            if kpoint_path is None:
                return self.exit_codes.ERROR_INVALID_INPUT_KPOINT_PATH

    def should_run_relax(self):
        """If the 'relax' input namespace was specified, we relax the input structure."""
        return 'relax' in self.inputs

    def run_relax(self):
        """Run the PwRelaxWorkChain to run a relax calculation"""
        inputs = AttributeDict(
            self.exposed_inputs(PwRelaxWorkChain, namespace='relax')
        )
        inputs.structure = self.ctx.current_structure
        inputs.metadata.call_link_label = 'relax'

        inputs = prepare_process_inputs(PwRelaxWorkChain, inputs)
        running = self.submit(PwRelaxWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(workchain_relax=running)

    def inspect_relax(self):
        """verify that the PwRelaxWorkChain successfully finished."""
        workchain = self.ctx.workchain_relax

        if not workchain.is_finished_ok:
            self.report(
                f'{workchain.process_label} failed with exit status {workchain.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_RELAX

        self.ctx.current_structure = workchain.outputs.output_structure

    def should_run_scf(self):
        """If the 'scf' input namespace was specified, we run the scf workchain."""
        return 'scf' in self.inputs

    def run_scf(self):
        """Run the PwBaseWorkChain in scf mode on the (optionally relaxed) input structure."""
        inputs = AttributeDict(
            self.exposed_inputs(PwBaseWorkChain, namespace='scf')
        )
        inputs.pw.structure = self.ctx.current_structure
        inputs.metadata.call_link_label = 'scf'

        inputs = prepare_process_inputs(PwBaseWorkChain, inputs)
        running = self.submit(PwBaseWorkChain, **inputs)
        self.report(
            f'launching {running.process_label}<{running.pk}> in scf mode'
        )

        return ToContext(workchain_scf=running)

    def inspect_scf(self):
        """Verify that the PwBaseWorkChain for the scf run successfully finished."""
        workchain = self.ctx.workchain_scf

        if not workchain.is_finished_ok:
            self.report(
                f'scf {workchain.process_label} failed with exit status {workchain.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_SCF

        self.ctx.current_folder = workchain.outputs.remote_folder

    def should_run_nscf(self):
        """If the 'nscf' input namespace was specified, we run the nscf workchain."""
        return 'nscf' in self.inputs

    def run_nscf(self):
        """Run the PwBaseWorkChain in nscf mode"""
        inputs = AttributeDict(
            self.exposed_inputs(PwBaseWorkChain, namespace='nscf')
        )
        inputs.pw.structure = self.ctx.current_structure
        inputs.pw.parent_folder = self.ctx.current_folder
        inputs.metadata.call_link_label = 'nscf'

        inputs = prepare_process_inputs(PwBaseWorkChain, inputs)
        running = self.submit(PwBaseWorkChain, **inputs)
        self.report(
            f'launching {running.process_label}<{running.pk}> in nscf mode'
        )

        return ToContext(workchain_nscf=running)

    def inspect_nscf(self):
        """Verify that the PwBaseWorkChain for the nscf run successfully finished."""
        workchain = self.ctx.workchain_nscf

        if not workchain.is_finished_ok:
            self.report(
                f'nscf {workchain.process_label} failed with exit status {workchain.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_NSCF

        self.ctx.current_folder = workchain.outputs.remote_folder

    def should_run_projwfc(self):
        """If the 'projwfc' input namespace was specified, we run the projwfc calculation."""
        return 'projwfc' in self.inputs

    def run_projwfc(self):
        """Projwfc step"""
        inputs = AttributeDict(
            self.exposed_inputs(ProjwfcCalculation, namespace='projwfc')
        )
        inputs.parent_folder = self.ctx.current_folder
        inputs.metadata.call_link_label = 'projwfc'

        inputs = prepare_process_inputs(ProjwfcCalculation, inputs)
        running = self.submit(ProjwfcCalculation, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(calc_projwfc=running)

    def inspect_projwfc(self):
        """Verify that the ProjwfcCalculation for the projwfc run successfully finished."""
        calculation = self.ctx.calc_projwfc

        if not calculation.is_finished_ok:
            self.report(
                f'{calculation.process_label} failed with exit status {calculation.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_PROJWFC

    def prepare_wannier90_inputs(self):
        """The input of wannier90 calculation is build here.
        Here it is separated out from `run_wannier90_pp`, so it can be overridden by subclasses."""
        inputs = AttributeDict(
            self.exposed_inputs(Wannier90Calculation, namespace='wannier90')
        )
        inputs.structure = self.ctx.current_structure
        parameters = inputs.parameters.get_dict()
        
        # Need fermi energy to shift the windows
        fermi_energy = None
        if self.inputs['relative_dis_windows']:
            if 'workchain_scf' not in self.ctx:
                raise ValueError(
                    "relative_dis_windows = True but did not run scf calculation"
                )
            scf_output_parameters = self.ctx.workchain_scf.outputs.output_parameters
            fermi_energy = get_fermi_energy(scf_output_parameters)
            if fermi_energy is None:
                raise ValueError(
                    "relative_dis_windows = True but cannot retrieve Fermi energy from scf output"
                )

        # add scf Fermi energy
        if 'workchain_scf' in self.ctx:
            scf_output_parameters = self.ctx.workchain_scf.outputs.output_parameters
            fermi_energy = get_fermi_energy(scf_output_parameters)
            if fermi_energy is None:
                raise ValueError(
                    "relative_dis_windows = True but cannot retrieve Fermi energy from scf output"
                )
            if self.inputs.relative_dis_windows:
                keys = [
                    'dis_froz_min', 'dis_froz_max', 'dis_win_min',
                    'dis_win_max'
                ]
                for k in keys:
                    v = parameters.get(k, None)
                    if v is not None:
                        parameters[k] += fermi_energy

        # set dis_froz_max
        if 'auto_froz_max' in self.inputs:
            bands = self.ctx.calc_projwfc.outputs.bands
            projections = self.ctx.calc_projwfc.outputs.projections
            args = {
                'bands': bands,
                'projections': projections
            }
            if 'auto_froz_max_threshold' in self.inputs:
                args['thresholds'] = self.inputs.auto_froz_max_threshold.value
            dis_froz_max = get_energy_of_projectability(**args)
            parameters['dis_froz_max'] = dis_froz_max

        if 'dis_froz_max' in parameters:
            bands = self.ctx.calc_projwfc.outputs.bands
            # TODO check provenance graph
            # dis_windows: More states in the frozen window than target WFs
            max_energy = np.min(
                bands.get_bands()[:, parameters['num_wann'] - 1]
            )
            dis_froz_max = min(max_energy, parameters['dis_froz_max'])
            if dis_froz_max != parameters['dis_froz_max']:
                parameters['dis_froz_max'] = dis_froz_max
        
        inputs.parameters = orm.Dict(dict=parameters)

        return inputs

    def run_wannier90_pp(self):
        inputs = self.prepare_wannier90_inputs()

        # add postproc
        if 'settings' in inputs:
            settings = inputs['settings'].get_dict()
        else:
            settings = {}
        settings['postproc_setup'] = True
        inputs['settings'] = settings

        inputs = {
            'wannier90': inputs,
            'metadata': {
                'call_link_label': 'wannier90_pp'
            }
        }
        inputs = prepare_process_inputs(Wannier90BaseWorkChain, inputs)

        running = self.submit(Wannier90BaseWorkChain, **inputs)
        self.report(
            f'launching {running.process_label}<{running.pk}> in postproc mode'
        )

        return ToContext(workchain_wannier90_pp=running)

    def inspect_wannier90_pp(self):
        """Verify that the Wannier90Calculation for the wannier90 run successfully finished."""
        workchain = self.ctx.workchain_wannier90_pp

        if not workchain.is_finished_ok:
            self.report(
                f'wannier90 postproc {workchain.process_label} failed with exit status {workchain.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_WANNIER90PP

    def prepare_pw2wannier90_inputs(self):
        inputs = AttributeDict(
            self.exposed_inputs(
                Pw2wannier90Calculation, namespace='pw2wannier90'
            )
        )

        inputs['parent_folder'] = self.ctx.current_folder
        inputs['nnkp_file'] = self.ctx.workchain_wannier90_pp.outputs.nnkp_file

        inputpp = inputs.parameters.get_dict().get('inputpp', {})
        scdm_proj = inputpp.get('scdm_proj', False)
        scdm_entanglement = inputpp.get('scdm_entanglement', None)
        scdm_mu = inputpp.get('scdm_mu', None)
        scdm_sigma = inputpp.get('scdm_sigma', None)

        calc_scdm_params = scdm_proj and scdm_entanglement == 'erfc'
        calc_scdm_params = calc_scdm_params and (
            scdm_mu is None or scdm_sigma is None
        )

        if scdm_entanglement == 'gaussian':
            if scdm_mu is None or scdm_sigma is None:
                raise ValueError(
                    "scdm_entanglement = gaussian but scdm_mu or scdm_sigma is empty."
                )

        if calc_scdm_params:
            if 'calc_projwfc' not in self.ctx:
                raise ValueError(
                    'Needs to run projwfc before auto-generating scdm_mu/sigma'
                )
            try:
                args = {
                    'parameters':
                    inputs.parameters,
                    'bands':
                    self.ctx.calc_projwfc.outputs.bands,
                    'projections':
                    self.ctx.calc_projwfc.outputs.projections,
                    'thresholds':
                    orm.Dict(
                        dict={
                            'max_projectability': 0.95,
                            'sigma_factor': 3
                        }
                    ),
                    'metadata': {
                        'call_link_label': 'update_scdm_mu_sigma'
                    }
                }
                inputs.parameters = update_scdm_mu_sigma(**args)
            except Exception as e:
                raise ValueError(f'update_scdm_mu_sigma failed! {e.args}')

        return inputs

    def run_pw2wannier90(self):
        inputs = self.prepare_pw2wannier90_inputs()
        inputs.metadata.call_link_label = 'pw2wannier90'

        inputs = prepare_process_inputs(Pw2wannier90Calculation, inputs)
        running = self.submit(Pw2wannier90Calculation, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(calc_pw2wannier90=running)

    def inspect_pw2wannier90(self):
        """Verify that the PwBaseWorkChain for the wannier90 run successfully finished."""
        workchain = self.ctx.calc_pw2wannier90

        if not workchain.is_finished_ok:
            self.report(
                f'{workchain.process_label} failed with exit status {workchain.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_PW2WANNIER90

        self.ctx.current_folder = workchain.outputs.remote_folder

    def run_wannier90(self):
        inputs = AttributeDict(
            self.exposed_inputs(Wannier90Calculation, namespace='wannier90')
        )
        inputs.metadata.call_link_label = 'wannier90'

        inputs['remote_input_folder'] = self.ctx.current_folder

        # use the Wannier90BaseWorkChain-corrected parameters
        # sort by pk, since the last Wannier90Calculation in Wannier90BaseWorkChain
        # should have the largest pk
        last_calc = max(
            self.ctx.workchain_wannier90_pp.called, key=lambda calc: calc.pk
        )
        # copy postproc inputs, especially the `kmesh_tol` might have been corrected
        for key in last_calc.inputs:
            inputs[key] = last_calc.inputs[key]

        if 'settings' in inputs:
            settings = inputs.settings.get_dict()
        else:
            settings = {}
        settings['postproc_setup'] = False

        inputs.settings = settings

        inputs = prepare_process_inputs(Wannier90Calculation, inputs)
        running = self.submit(Wannier90Calculation, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(calc_wannier90=running)

    def inspect_wannier90(self):
        """Verify that the PwBaseWorkChain for the wannier90 run successfully finished."""
        workchain = self.ctx.calc_wannier90

        if not workchain.is_finished_ok:
            self.report(
                f'{workchain.process_label} failed with exit status {workchain.exit_status}'
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_WANNIER90

        self.ctx.current_folder = workchain.outputs.remote_folder

    def results(self):
        """Attach the desired output nodes directly as outputs of the workchain"""
        if 'workchain_relax' in self.ctx:
            self.out_many(
                self.exposed_outputs(
                    self.ctx.workchain_relax,
                    PwRelaxWorkChain,
                    namespace='relax'
                )
            )

        if 'workchain_scf' in self.ctx:
            self.out_many(
                self.exposed_outputs(
                    self.ctx.workchain_scf, PwBaseWorkChain, namespace='scf'
                )
            )

        if 'workchain_nscf' in self.ctx:
            self.out_many(
                self.exposed_outputs(
                    self.ctx.workchain_nscf, PwBaseWorkChain, namespace='nscf'
                )
            )

        if 'calc_projwfc' in self.ctx:
            self.out_many(
                self.exposed_outputs(
                    self.ctx.calc_projwfc,
                    ProjwfcCalculation,
                    namespace='projwfc'
                )
            )

        self.out_many(
            self.exposed_outputs(
                self.ctx.calc_pw2wannier90,
                Pw2wannier90Calculation,
                namespace='pw2wannier90'
            )
        )
        self.out_many(
            self.exposed_outputs(
                self.ctx.workchain_wannier90_pp,
                Wannier90BaseWorkChain,
                namespace='wannier90_pp'
            )
        )
        self.out_many(
            self.exposed_outputs(
                self.ctx.calc_wannier90,
                Wannier90Calculation,
                namespace='wannier90'
            )
        )

        # not necessary but it is good to do some sanity checks: 
        # 1. the calculated number of projections is consistent with QE projwfc.x
        from ..utils.upf import get_number_of_electrons, get_number_of_projections
        args = {
            'structure': self.ctx.current_structure,
            # the type of `self.inputs['scf']['pw']['pseudos']` is plumpy.utils.AttributesFrozendict,
            # we need to convert it to dict, otherwise get_number_of_projections will fail.
            'pseudos': dict(self.inputs['scf']['pw']['pseudos'])
        }
        if 'calc_projwfc' in self.ctx:
            num_proj = len(
                self.ctx.calc_projwfc.outputs['projections'].get_orbitals()
            )
            number_of_projections = get_number_of_projections(**args)
            if number_of_projections != num_proj:
                raise ValueError(
                    f'number of projections {number_of_projections} != projwfc.x output {num_proj}'
                )
        # 2. the number of electrons is consistent with QE output
        num_elec = self.ctx.workchain_scf.outputs['output_parameters']['number_of_electrons']
        number_of_electrons = get_number_of_electrons(**args)
        if number_of_electrons != num_elec:
            raise ValueError(
                f'number of electrons {number_of_electrons} != QE output {num_elec}'
            )

        self.report(f'{self.get_name()} successfully completed')

    def on_terminated(self):
        """Clean the working directories of all child calculations if `clean_workdir=True` in the inputs."""
        super().on_terminated()

        if not self.inputs.clean_workdir:
            self.report('remote folders will not be cleaned')
            return

        cleaned_calcs = []

        for called_descendant in self.node.called_descendants:
            if isinstance(called_descendant, orm.CalcJobNode):
                try:
                    called_descendant.outputs.remote_folder._clean()  # pylint: disable=protected-access
                    cleaned_calcs.append(called_descendant.pk)
                except (IOError, OSError, KeyError):
                    pass

        if cleaned_calcs:
            self.report(f"cleaned remote folders of calculations: {' '.join(map(str, cleaned_calcs))}")

    @classmethod
    def get_protocol_filepath(cls):
        """Return ``pathlib.Path`` to the ``.yaml`` file that defines the protocols."""
        from importlib_resources import files
        from . import protocols as wannier_protocols
        return files(wannier_protocols) / 'wannier.yaml'

    @classmethod
    def get_relax_inputs(cls, code, kpoints_distance, **kwargs):
        overrides = {
            'clean_workdir': False,
            'base': {
                'kpoints_distance': kpoints_distance
            }
        }
        builder = PwRelaxWorkChain.get_builder_from_protocol(
            code=code, overrides=overrides, **kwargs
        )

        excluded_inputs = ['clean_workdir', 'structure']
        inputs = {}
        for input in builder:
            if input in excluded_inputs:
                continue
            inputs[input] = builder[input]

        return inputs

    @classmethod
    def get_scf_inputs(cls, code, kpoints_distance, **kwargs):
        overrides = {
            'clean_workdir': False,
            'kpoints_distance': kpoints_distance
        }
        builder = PwBaseWorkChain.get_builder_from_protocol(
            code=code, overrides=overrides, **kwargs
        )

        parameters = builder.pw['parameters'].get_dict()
        if kwargs.get('spin_type', None) == SpinType.NON_COLLINEAR:
            parameters['SYSTEM']['noncolin'] = True
        if kwargs.get('spin_type', None) == SpinType.SPIN_ORBIT:
            parameters['SYSTEM']['noncolin'] = True
            parameters['SYSTEM']['lspinorb'] = True
        builder.pw['parameters'] = orm.Dict(dict=parameters)

        # Currently only support magnetic with SOC
        # for magnetic w/o SOC, needs 2 separate wannier90 calculations for spin up and down.
        # if self.inputs.spin_polarized and self.inputs.spin_orbit_coupling:
        #     # Magnetization from Kittel, unit: Bohr magneton
        #     magnetizations = {'Fe': 2.22, 'Co': 1.72, 'Ni': 0.606}
        #     from aiida_wannier90_workflows.utils.upf import get_number_of_electrons_from_upf
        #     for i, kind in enumerate(self.inputs.structure.kinds):
        #         if kind.name in magnetizations:
        #             zvalence = get_number_of_electrons_from_upf(
        #                 self.ctx.pseudos[kind.name]
        #             )
        #             spin_polarization = magnetizations[kind.name] / zvalence
        #             pw_parameters['SYSTEM'][f"starting_magnetization({i+1})"
        #                                     ] = spin_polarization

        excluded_inputs = ['clean_workdir']
        inputs = {}
        for input in builder:
            if input in excluded_inputs:
                continue
            inputs[input] = builder[input]
        # structure is in the pw namespace, I need to pop it
        inputs['pw'].pop('structure', None)

        return inputs

    @classmethod
    def get_nscf_inputs(cls, code, kpoints_distance, nbands_factor, **kwargs):
        overrides = {
            'clean_workdir': False,
            'kpoints_distance': kpoints_distance
        }
        builder = PwBaseWorkChain.get_builder_from_protocol(
            code=code, overrides=overrides, **kwargs
        )

        parameters = builder.pw.parameters.get_dict()

        only_valence = kwargs['electronic_type'] == ElectronicType.INSULATOR
        spin_polarized = kwargs['spin_type'] == SpinType.COLLINEAR
        nbnd = get_wannier_number_of_bands(
            structure=kwargs['structure'],
            pseudos=builder.pw.pseudos,
            factor=nbands_factor,
            only_valence=only_valence,
            spin_polarized=spin_polarized
        )
        parameters['SYSTEM']['nbnd'] = nbnd

        parameters['SYSTEM']['nosym'] = True
        parameters['SYSTEM']['noinv'] = True
        
        if kwargs.get('spin_type', None) == SpinType.NON_COLLINEAR:
            parameters['SYSTEM']['noncolin'] = True
        if kwargs.get('spin_type', None) == SpinType.SPIN_ORBIT:
            parameters['SYSTEM']['noncolin'] = True
            parameters['SYSTEM']['lspinorb'] = True
        builder.pw['parameters'] = orm.Dict(dict=parameters)

        parameters['CONTROL']['restart_mode'] = 'restart'
        parameters['CONTROL']['calculation'] = 'nscf'
        # TODO switch to david?
        parameters['ELECTRONS']['diagonalization'] = 'cg'
        parameters['ELECTRONS']['diago_full_acc'] = True

        builder.pw.parameters = orm.Dict(dict=parameters)

        excluded_inputs = ['clean_workdir', 'structure']
        inputs = {}
        for input in builder:
            if input in excluded_inputs:
                continue
            inputs[input] = builder[input]
        # structure is in the pw namespace, I need to pop it
        inputs['pw'].pop('structure', None)

        # use explicit list of kpoints, since auto generated kpoints
        # maybe different between QE & Wannier90. Here we explicitly
        # generate a list of kpoint to avoid discrepencies between
        # QE's & Wannier90's automatically generated kpoints.
        kpoints = get_explicit_kpoints_from_distance(
            kwargs['structure'], kpoints_distance
        )
        inputs.pop('kpoints_distance', None)
        inputs['kpoints'] = kpoints

        return inputs

    @classmethod
    def get_projwfc_inputs(cls, code, **kwargs):
        parameters = orm.Dict(dict={'PROJWFC': {'DeltaE': 0.2}})

        inputs = {
            'code': code,
            'parameters': parameters,
            'metadata': {
                'options': {
                    'resources': {
                        'num_machines': 1
                    }
                },
            }
        }

        return inputs

    @classmethod
    def get_pw2wannier90_inputs(
        cls,
        code,
        *,
        projection_type,
        exclude_pswfcs=None,
        write_unk=False,
        **kwargs
    ):
        """Here no need to set scdm_mu, scdm_sigma"""
        parameters = {
            'write_mmn': True,
            'write_amn': True,
        }
        # write UNK files (to plot WFs)
        if write_unk:
            parameters['write_unk'] = True

        if projection_type == WannierProjectionType.SCDM:
            parameters['scdm_proj'] = True

            if kwargs['electronic_type'] == ElectronicType.INSULATOR:
                parameters['scdm_entanglement'] = 'isolated'
            else:
                parameters['scdm_entanglement'] = 'erfc'
                # scdm_mu, scdm_sigma will be set after projwfc run
        elif projection_type == WannierProjectionType.NUMERIC:
            parameters['use_pao'] = True
            parameters['ortho_paos'] = True
            if exclude_pswfcs is not None:
                parameters['exclude_paos'] = list(exclude_pswfcs)

        parameters = orm.Dict(dict={'inputpp': parameters})
        inputs = {
            'code': code,
            'parameters': parameters,
            'metadata': {
                'options': {
                    'resources': {
                        'num_machines': 1
                    }
                },
            }
        }
        return inputs

    @classmethod
    def get_wannier90_inputs(
        cls,
        code,
        *,
        projection_type,
        disentanglement_type,
        kpoints_distance,
        nbands,
        pseudos,
        maximal_localisation=None,
        exclude_semicores=True,
        plot_wannier_functions=False,
        retrieve_hamiltonian=False,
        retrieve_matrices=False,
        **kwargs
    ):

        inputs = {
            'code': code,
            'settings': {},
        }
        parameters = {
            'use_ws_distance': True,
        }

        structure = kwargs['structure']

        # Set num_bands, num_wann, also take care of semicore states
        parameters['num_bands'] = nbands
        num_projs = get_number_of_projections(structure, pseudos)

        # TODO check nospin, spin, soc
        if kwargs['electronic_type'] == ElectronicType.INSULATOR:
            num_wann = parameters['num_bands']
        else:
            num_wann = num_projs

        pseudo_orbitals = get_pseudo_orbitals(pseudos)
        if exclude_semicores:
            # TODO now only consider SSSP
            semicore_list = get_semicore_list(structure, pseudo_orbitals)
            num_excludes = len(semicore_list)
            # I assume all the semicore bands are the lowest
            exclude_pswfcs = range(1, num_excludes + 1)
            if num_excludes != 0:
                parameters['exclude_bands'] = exclude_pswfcs
                num_wann -= num_excludes
                parameters['num_bands'] -= num_excludes

        if num_wann <= 0:
            raise ValueError(f"Wrong num_wann {num_wann}")
        parameters['num_wann'] = num_wann

        # Set projections
        if projection_type == WannierProjectionType.SCDM:
            parameters['auto_projections'] = True
        elif projection_type == WannierProjectionType.NUMERIC:
            parameters['auto_projections'] = True
        elif projection_type == WannierProjectionType.HYDROGEN:
            projections = []
            # TODO
            # self.ctx.wannier_projections = orm.List(
            #     list=get_projections(**args)
            # )
            for site in structure.sites:
                for orb in pseudo_orbitals[site.kind_name]['pswfcs']:
                    if exclude_semicores:
                        if orb in pseudo_orbitals[site.kind_name]['semicores']:
                            continue
                    projections.append(f'{site.kind_name}:{orb[-1].lower()}')
            inputs['projections'] = projections
        elif projection_type == WannierProjectionType.RANDOM:
            inputs['settings'].update({'random_projections': True})
        else:
            raise ValueError(f"Unrecognized projection type {projection_type}")

        if kwargs['spin_type'] in [
            SpinType.NON_COLLINEAR, SpinType.SPIN_ORBIT
        ]:
            parameters['spinors'] = True

        if plot_wannier_functions:
            parameters['wannier_plot'] = True

        default_num_iter = 4000
        num_atoms = len(structure.sites)
        if maximal_localisation:
            parameters.update({
                'num_iter': default_num_iter,
                'conv_tol': 1e-7 * num_atoms,
                'conv_window': 3,
            })
        else:
            parameters['num_iter'] = 0

        default_dis_num_iter = 4000
        if disentanglement_type == WannierDisentanglementType.NONE:
            parameters['dis_num_iter'] = 0
        elif disentanglement_type == WannierDisentanglementType.WINDOW_FIXED:
            parameters.update({
                'dis_num_iter': default_dis_num_iter,
                'dis_conv_tol': parameters['conv_tol'],
                # Here +2 means fermi_energy + 2 eV, however Fermi energy is calculated when Wannier90WorkChain is running,
                # so it will add Fermi energy with this dis_froz_max dynamically.
                'dis_froz_max': +2.0,
            })
        elif disentanglement_type == WannierDisentanglementType.WINDOW_AUTO:
            # WINDOW_AUTO needs projectability, will be set dynamically when workchain is running
            parameters.update({
                'dis_num_iter': default_dis_num_iter,
                'dis_conv_tol': parameters['conv_tol'],
            })
        elif disentanglement_type == WannierDisentanglementType.PROJECTABILITY:
            parameters.update({
                'dis_num_iter': default_dis_num_iter,
                'dis_conv_tol': parameters['conv_tol'],
                'dis_proj_min': 0.01,
                'dis_proj_min': 0.95,
            })
        elif disentanglement_type == WannierDisentanglementType.WINDOW_AND_PROJECTABILITY:
            parameters.update({
                'dis_num_iter': default_dis_num_iter,
                'dis_conv_tol': parameters['conv_tol'],
                'dis_proj_min': 0.01,
                'dis_proj_min': 0.95,
                'dis_froz_max': +2.0, # relative to fermi_energy
            })
        else:
            raise ValueError(f"Not supported disentanglement type: {disentanglement_type}")

        if retrieve_hamiltonian:
            parameters['write_tb'] = True
            parameters['write_hr'] = True
            parameters['write_xyz'] = True

        # if inputs.kpoints is a kmesh, mp_grid will be auto-set,
        # otherwise we need to set it manually
        # if self.inputs.use_opengrid:
        # kpoints will be set dynamically after opengrid calculation,
        # the self.ctx.nscf_kpoints won't be used.
        # inputs['kpoints'] = self.ctx.nscf_kpoints
        # else:
        kpoints = create_kpoints_from_distance(structure, kpoints_distance)
        inputs['kpoints'] = get_explicit_kpoints(kpoints)
        parameters['mp_grid'] = kpoints.get_kpoints_mesh()[0]

        inputs['parameters'] = orm.Dict(dict=parameters)
        inputs['metadata'] = {'options': {'resources': {'num_machines': 1}}}

        if retrieve_hamiltonian:
            # tbmodels needs aiida.win file
            inputs['settings'].update({'additional_retrieve_list': ['*.win']})

        if retrieve_matrices:
            # also retrieve .chk file in case we need it later
            seedname = Wannier90Calculation._DEFAULT_INPUT_FILE.split('.')[0]
            retrieve_list = inputs['settings']['additional_retrieve_list']
            retrieve_list += [
                '{}.{}'.format(seedname, ext)
                for ext in ['chk', 'eig', 'amn', 'mmn', 'spn']
            ]
            inputs['settings']['additional_retrieve_list'] = retrieve_list
        # I need to convert settings into orm.Dict
        inputs['settings'] = orm.Dict(dict=inputs['settings'])

        return inputs

    @classmethod
    def get_builder_from_protocol(
        cls,
        codes: dict,
        structure: orm.StructureData,
        *,
        protocol: str = None,
        overrides: dict = None,
        projection_type: WannierProjectionType = WannierProjectionType.SCDM,
        disentanglement_type: WannierDisentanglementType = WannierDisentanglementType.AUTO,
        maximal_localisation: bool = True,
        exclude_semicores: bool = True,
        plot_wannier_functions: bool = False,
        retrieve_hamiltonian: bool = False,
        retrieve_matrices: bool = False,
        electronic_type: ElectronicType = ElectronicType.METAL,
        spin_type: SpinType = SpinType.NONE,
        initial_magnetic_moments: dict = None,
        print_summary: bool = True,
        **_
    ) -> ProcessBuilder:
        """Return a builder prepopulated with inputs selected according to the chosen protocol.

        :param codes: a dictionary of ``Code`` instance for pw.x, pw2wannier90.x, wannier90.x, (optionally) projwfc.x.
        :type codes: dict
        :param structure: the ``StructureData`` instance to use.
        :type structure: orm.StructureData
        :param protocol: protocol to use, if not specified, the default will be used.
        :type protocol: str
        :param overrides: optional dictionary of inputs to override the defaults of the protocol.
        :param projection_type: indicate the Wannier initial projection type of the system through ``WannierProjectionType`` instance.
        :param disentanglement_type: indicate the Wannier disentanglement type of the system through ``WannierDisentanglementType`` instance.
        :param maximal_localisation: if true do maximal localisation of Wannier functions.
        :param exclude_semicores: if True do not Wannierise semicore states.
        :param plot_wannier_functions: if True plot Wannier functions as xsf files.
        :param retrieve_hamiltonian: if True retrieve Wannier Hamiltonian.
        :param retrieve_matrices: if True retrieve amn/mmn/eig/chk/spin files.
        :param electronic_type: indicate the electronic character of the system through ``ElectronicType`` instance.
        :param spin_type: indicate the spin polarization type to use through a ``SpinType`` instance.
        :param initial_magnetic_moments: optional dictionary that maps the initial magnetic moment of each kind to a
            desired value for a spin polarized calculation. Note that for ``spin_type == SpinType.COLLINEAR`` an initial
            guess for the magnetic moment is automatically set in case this argument is not provided.
        :param print_summary: if True print a summary of key input parameters
        :return: a process builder instance with all inputs defined and ready for launch.
        :rtype: ProcessBuilder
        """
        from aiida_quantumespresso.workflows.protocols.utils import get_starting_magnetization

        # This will be used in various WorkChain.get_builder_from_protocol(...)
        keywordargs = dict(
            structure=structure,
            protocol=protocol,
            electronic_type=electronic_type,
            spin_type=spin_type,
            initial_magnetic_moments=initial_magnetic_moments
        )

        # A dictionary containing key info of Wannierisation and will be printed when the function returns.
        summary = {}

        CODES_REQUIRED_KEYS = ['pw', 'pw2wannier90', 'wannier90']
        CODES_OPTINAL_KEYS = ['projwfc', 'opengrid']
        if not isinstance(codes, dict):
            msg = f"`codes` must be a dictionary containing at least the following keys: {', '.join(CODES_REQUIRED_KEYS)}"
            raise TypeError(msg)
        for k in CODES_REQUIRED_KEYS:
            if k not in codes.keys():
                raise ValueError(
                    f"`codes` does not contain the required key: {k}"
                )
        for k, code in codes.items():
            if isinstance(code, str):
                code = orm.load_code(code)
                type_check(code, orm.Code)
                codes[k] = code

        type_check(electronic_type, ElectronicType)
        type_check(spin_type, SpinType)

        if electronic_type not in [
            ElectronicType.METAL, ElectronicType.INSULATOR
        ]:
            raise NotImplementedError(
                f'electronic type `{electronic_type}` is not supported.'
            )

        if spin_type not in [SpinType.NONE]:  #, SpinType.COLLINEAR]:
            raise NotImplementedError(
                f'spin type `{spin_type}` is not supported.'
            )

        if initial_magnetic_moments is not None and spin_type is not SpinType.COLLINEAR:
            raise ValueError(
                f'`initial_magnetic_moments` is specified but spin type `{spin_type}` is incompatible.'
            )

        summary['Formula'] = structure.get_formula()
        summary['ElectronicType'] = electronic_type
        summary['SpinType'] = spin_type
        summary['WannierProjectionType'] = projection_type

        inputs = cls.get_protocol_inputs(protocol, overrides)
        inputs = AttributeDict(inputs)
        builder = cls.get_builder()
        builder.structure = structure

        kpoints_distance = inputs.pop('kpoints_distance')
        nbands_factor = inputs.pop('nbands_factor')

        # pseudo_family = 's'
        # try:
        #     SsspFamily = GroupFactory('pseudo.family.sssp')
        #     PseudoDojoFamily = GroupFactory('pseudo.family.pseudo_dojo')
        #     CutoffsPseudoPotentialFamily = GroupFactory('pseudo.family.cutoffs')
        #     pseudo_set = (PseudoDojoFamily, SsspFamily, CutoffsPseudoPotentialFamily)
        #     pseudo_family = orm.QueryBuilder().append(pseudo_set, filters={'label': pseudo_family}).one()[0]
        # except exceptions.NotExistent as exception:
        #     raise ValueError(
        #         f'required pseudo family `{pseudo_family}` is not installed. Please use `aiida-pseudo install` to'
        #         'install it.'
        #     ) from exception

        # try:
        #     cutoff_wfc, cutoff_rho = pseudo_family.get_recommended_cutoffs(structure=structure, unit='Ry')
        # except ValueError as exception:
        #     raise ValueError(
        #         f'failed to obtain recommended cutoffs for pseudo family `{pseudo_family}`: {exception}'
        #     ) from exception

        # relax
        if inputs.get('relax', False):
            builder.relax = cls.get_relax_inputs(
                codes['pw'], kpoints_distance, **keywordargs
            )

        # scf
        if inputs.get('scf', True):
            builder.scf = cls.get_scf_inputs(
                codes['pw'], kpoints_distance, **keywordargs
            )

        # nscf
        if inputs.get('nscf', True):
            builder.nscf = cls.get_nscf_inputs(
                codes['pw'], kpoints_distance, nbands_factor, **keywordargs
            )

        # projwfc
        run_projwfc = inputs.get('projwfc', False)
        if projection_type == WannierProjectionType.SCDM:
            run_projwfc = True
        if disentanglement_type == WannierDisentanglementType.WINDOW_AUTO:
            run_projwfc = True
        if run_projwfc:
            builder.projwfc = cls.get_projwfc_inputs(
                codes['projwfc'], **keywordargs
            )

        # pw2wannier90
        if inputs.get('pw2wannier90', True):
            exclude_pswfcs = None
            if exclude_semicores:
                pseudo_orbitals = get_pseudo_orbitals(
                    builder.scf['pw']['pseudos']
                )
                exclude_pswfcs = get_semicore_list(structure, pseudo_orbitals)
            pw2wannier_inputs = cls.get_pw2wannier90_inputs(
                code=codes['pw2wannier90'],
                projection_type=projection_type,
                exclude_pswfcs=exclude_pswfcs,
                plot_wannier_functions=plot_wannier_functions,
                **keywordargs
            )
            builder.pw2wannier90 = pw2wannier_inputs

        # wannier90
        if inputs.get('wannier90', True):
            if disentanglement_type == WannierDisentanglementType.AUTO:
                if electronic_type == ElectronicType.INSULATOR:
                    disentanglement_type = WannierDisentanglementType.NONE
                else:
                    if projection_type in [WannierProjectionType.HYDROGEN, WannierProjectionType.RANDOM]:
                        disentanglement_type = WannierDisentanglementType.WINDOW_FIXED
                    elif projection_type == WannierProjectionType.NUMERIC:
                        disentanglement_type = WannierDisentanglementType.WINDOW_AND_PROJECTABILITY
                    elif projection_type == WannierProjectionType.SCDM:
                        # No disentanglement when using SCDM, otherwise the wannier interpolated bands are wrong
                        disentanglement_type = WannierDisentanglementType.NONE
                    else:
                        raise ValueError(f"Cannot automatically guess disentanglement type from projection type: {projection_type}")
            summary['WannierDisentanglementType'] = disentanglement_type
            wannier_inputs = cls.get_wannier90_inputs(
                code=codes['wannier90'],
                projection_type=projection_type,
                kpoints_distance=kpoints_distance,
                nbands=builder.nscf['pw']['parameters']['SYSTEM']['nbnd'],
                pseudos=builder.scf['pw']['pseudos'],
                disentanglement_type=disentanglement_type,
                maximal_localisation=maximal_localisation,
                exclude_semicores=exclude_semicores,
                plot_wannier_functions=plot_wannier_functions,
                retrieve_hamiltonian=retrieve_hamiltonian,
                retrieve_matrices=retrieve_matrices,
                **keywordargs
            )
            builder.wannier90 = wannier_inputs
            builder.relative_dis_windows = orm.Bool(True)
        
        builder.clean_workdir = orm.Bool(inputs.get('clean_workdir', False))

        summary['num_bands'] = builder.wannier90['parameters']['num_bands']
        summary['num_wann'] = builder.wannier90['parameters']['num_wann']
        summary['exclude_bands'] = builder.wannier90['parameters'
                                                     ]['exclude_bands']
        summary['mp_grid'] = builder.wannier90['parameters']['mp_grid']

        if print_summary:
            # try to pretty print
            print("Summary of key input parameters:")
            for k, v in summary.items():
                print(f'  {k}: {v}')
            print('')
            print('Notes:')
            print(
                '  1. The `relative_dis_windows` = True, meaning the `dis_froz/win_min/max` in the wannier90 input parameters will be shifted by Fermi energy from scf output parameters.'
            )
            print(
                '  2. If you set `scdm_mu` and/or `scdm_sigma` in the pw2wannier90 input parameters, the WorkChain will directly use the provided mu and/or sigma. The missing one will be generated from projectability.'
            )

        return builder


def get_fermi_energy(output_parameters: orm.Dict) -> typing.Optional[float]:
    """get Fermi energy from scf output parameters, unit is eV

    :param output_parameters: scf output parameters
    :type output_parameters: orm.Dict
    :return: if found return Fermi energy, else None
    :rtype: float, None
    """
    out_dict = output_parameters.get_dict()
    fermi = out_dict.get('fermi_energy', None)
    fermi_units = out_dict.get('fermi_energy_units', None)
    if fermi_units != 'eV':
        return None
    else:
        return fermi


@calcfunction
def update_scdm_mu_sigma(parameters: orm.Dict, bands: orm.BandsData, 
projections: orm.ProjectionData, thresholds: orm.Dict) -> orm.Dict:
    """Use erfc fitting to extract scdm_mu & scdm_sigma, and update the pw2wannier90 input parameters.
    If scdm_mu/sigma is provided in the input, then it will not be updated, only the missing one(s) will be updated.

    :param parameters: pw2wannier90 input parameters
    :type parameters: aiida.orm.Dict
    :param bands: band structure
    :type bands: aiida.orm.BandsData
    :param projections: projectability from projwfc.x
    :type projections: aiida.orm.ProjectionData
    :param thresholds: sigma shift factor
    :type thresholds: aiida.orm.Dict
    """
    parameters_dict = parameters.get_dict()
    mu_new, sigma_new = fit_scdm_mu_sigma_aiida(
        bands, projections, thresholds.get_dict()
    )
    scdm_parameters = {}
    if 'scdm_mu' not in parameters_dict['inputpp']:
        scdm_parameters['scdm_mu'] = mu_new
    if 'scdm_sigma' not in parameters_dict['inputpp']:
        scdm_parameters['scdm_sigma'] = sigma_new
    parameters_dict['inputpp'].update(scdm_parameters)
    return orm.Dict(dict=parameters_dict)


def get_pseudo_orbitals(pseudos: dict[str, UpfData]) -> dict:
    pseudo_data = _load_pseudo_metadata('semicore_sssp_efficiency_1.1.json')
    pseudo_orbitals = {}
    for element in pseudos:
        if pseudo_data[element]['md5'] != pseudos[element].md5:
            raise ValueError(
                f"Cannot find pseudopotential {element} with md5 {pseudo_data[element]['md5']}"
            )
        pseudo_orbitals[element] = pseudo_data[element]
    return pseudo_orbitals


def get_semicore_list(structure: orm.StructureData, pseudo_orbitals: dict) -> list:
    # pw2wannier90.x/projwfc.x store pseudo-wavefunctions in the same order
    # as ATOMIC_POSITIONS in pw.x input file; aiida-quantumespresso writes
    # ATOMIC_POSITIONS in the order of StructureData.sites.
    # Note some times the PSWFC in UPF files are not ordered, i.e. it's not
    # always true that the first several PSWFC are semicores states, the
    # json file which we loaded in the self.ctx.pseudo_pswfcs already
    # consider this ordering, e.g.
    # "Ce": {
    #     "filename": "Ce.GGA-PBE-paw-v1.0.UPF",
    #     "md5": "c46c5ce91c1b1c29a1e5d4b97f9db5f7",
    #     "pswfcs": ["5S", "6S", "5P", "6P", "5D", "6D", "4F", "5F"],
    #     "semicores": ["5S", "5P"]
    # }
    from copy import deepcopy
    label2num = {'S': 1, 'P': 3, 'D': 5, 'F': 7}
    semicore_list = []  # index should start from 1
    num_pswfcs = 0
    for site in structure.sites:
        # here I use deepcopy to make sure list.remove() does not
        # interfere with the original list.
        site_pswfcs = deepcopy(pseudo_orbitals[site.kind_name]['pswfcs'])
        site_semicores = deepcopy(pseudo_orbitals[site.kind_name]['semicores'])
        for orb in site_pswfcs:
            num_orbs = label2num[orb[-1]]
            if orb in site_semicores:
                site_semicores.remove(orb)
                semicore_list.extend(
                    list(range(num_pswfcs + 1, num_pswfcs + num_orbs + 1))
                )
            num_pswfcs += num_orbs
        if len(site_semicores) != 0:
            return ValueError(
                f"Error when processing pseudo {site.kind_name} with orbitals {pseudo_orbitals}"
            )
    return semicore_list
