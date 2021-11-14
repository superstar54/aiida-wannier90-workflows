# -*- coding: utf-8 -*-
"""Wrapper workchain for BaseRestartWorkChain to automatically handle several QE errors."""
import re
from aiida.common import AttributeDict
from aiida.engine import while_
from aiida.engine import BaseRestartWorkChain
from aiida.engine import process_handler, ProcessHandlerReport
from aiida_quantumespresso.calculations.namelists import NamelistsCalculation


class QeBaseRestartWorkChain(BaseRestartWorkChain):
    """Workchain to run a QE calculation with automated error handling and restarts.

    To handle Out-Of-Memory error, the `_process_class` needs to define the exit code
    ``ERROR_OUTPUT_STDOUT_INCOMPLETE``.
    """

    # When subclass this workchain, need to set these, e.g.
    # _process_class = Pw2wannier90Calculation
    # _expose_inputs_namespace = 'pw2wannier90'
    _process_class = NamelistsCalculation
    _expose_inputs_namespace = 'base'

    _mpi_proc_reduce_factor = 2

    @classmethod
    def define(cls, spec):
        """Define the process spec."""
        super().define(spec)
        spec.expose_inputs(cls._process_class, namespace=cls._expose_inputs_namespace)

        spec.outline(
            cls.setup,
            while_(cls.should_run_process)(
                cls.run_process,
                cls.inspect_process,
            ),
            cls.results,
        )

        spec.expose_outputs(cls._process_class)

        spec.exit_code(
            311,
            'ERROR_OUTPUT_STDOUT_INCOMPLETE',
            message='The stdout output file was incomplete probably because the calculation got interrupted.'
        )

    def setup(self):
        """Call the `setup` of the `BaseRestartWorkChain` and then create the inputs dictionary in `self.ctx.inputs`.

        This `self.ctx.inputs` dictionary will be used by the `BaseRestartWorkChain` to submit
        the calculations in the internal loop.
        """
        super().setup()
        self.ctx.inputs = AttributeDict(self.exposed_inputs(self._process_class, self._expose_inputs_namespace))

    def report_error_handled(self, calculation, action):
        """Report an action taken for a calculation that has failed.

        This should be called in a registered error handler if its condition is met and an action was taken.

        :param calculation: the failed calculation node
        :param action: a string message with the action taken
        """
        message = f'{calculation.process_label}<{calculation.pk}> failed'
        message += f' with exit status {calculation.exit_status}: {calculation.exit_message}'
        self.report(message)
        self.report(f'Action taken: {action}')

    @process_handler(exit_codes=[_process_class.exit_codes.ERROR_OUTPUT_STDOUT_INCOMPLETE])  # pylint: disable=no-member
    def handle_output_stdout_incomplete(self, calculation):
        """Try to fix incomplete stdout error by reducing the number of cores.

        Often the ERROR_OUTPUT_STDOUT_INCOMPLETE is due to out-of-memory.
        The handler will try to decrease `num_mpiprocs_per_machine` by `_mpi_proc_reduce_factor`.
        """
        regex = re.compile(r'Detected \d+ oom-kill event\(s\) in step')
        scheduler_stderr = calculation.get_scheduler_stderr()
        for line in scheduler_stderr.split('\n'):
            if regex.search(line) or 'Out Of Memory' in line:
                break
        else:
            action = 'Unrecoverable incomplete stdout error'
            self.report_error_handled(calculation, action)
            return ProcessHandlerReport(True, self.exit_codes.ERROR_OUTPUT_STDOUT_INCOMPLETE)

        metadata = self.ctx.inputs['metadata']
        current_num_mpiprocs_per_machine = metadata['options']['resources'].get('num_mpiprocs_per_machine', 1)
        # num_mpiprocs_per_machine = calculation.attributes['resources'].get('num_mpiprocs_per_machine', 1)

        if current_num_mpiprocs_per_machine == 1:
            action = 'Unrecoverable out-of-memory error after setting num_mpiprocs_per_machine to 1'
            self.report_error_handled(calculation, action)
            return ProcessHandlerReport(True, self.exit_codes.ERROR_OUTPUT_STDOUT_INCOMPLETE)

        new_num_mpiprocs_per_machine = current_num_mpiprocs_per_machine // self._mpi_proc_reduce_factor
        metadata['options']['resources']['num_mpiprocs_per_machine'] = new_num_mpiprocs_per_machine
        action = f'Out-of-memory error, current num_mpiprocs_per_machine = {current_num_mpiprocs_per_machine}'
        action += f', new num_mpiprocs_per_machine = {new_num_mpiprocs_per_machine}'
        self.report_error_handled(calculation, action)
        self.ctx.inputs['metadata'] = metadata

        if 'settings' in self.ctx.inputs:
            settings = self.ctx.inputs['settings'].get_dict()
            # {'cmdline': ['-nk', '16']}, I need to reduce it as well
            cmdline = settings.get('cmdline', None)
            if cmdline:
                for key in ('-nk', '-npools'):
                    if key in cmdline:
                        idx = cmdline.index(key)
                        if idx + 1 > len(cmdline) - 1:
                            # This should not happen, in this case the cmdline is wrong
                            continue
                        try:
                            cmdline[idx + 1] = f'{int(cmdline[idx + 1]) // 2}'
                        except ValueError:
                            continue

        return ProcessHandlerReport(True)
