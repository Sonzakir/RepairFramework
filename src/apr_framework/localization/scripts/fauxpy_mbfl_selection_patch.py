from importlib.util import find_spec
from pathlib import Path

spec = find_spec("fauxpy")
if spec is None or spec.origin is None:
    raise SystemExit("Cannot patch FauxPy because the fauxpy package is not importable.")

package_root = Path(spec.origin).parent


def read(path):
    return path.read_text(encoding="utf-8")


def write(path, text):
    path.write_text(text, encoding="utf-8")


def replace_once(text, old, new, label):
    if new in text:
        return text
    if old not in text:
        raise SystemExit(f"Cannot apply FauxPy MBFL selection patch: expected {label} was not found.")
    return text.replace(old, new, 1)


pytest_manager_path = package_root / "command_line" / "pytest_mode" / "pytest_option_manager.py"
pytest_text = read(pytest_manager_path)
pytest_text = replace_once(
    pytest_text,
    '''        group.addoption(
            "--fauxpy-verbose",
            action="store_true",
            default=False,
            help="Show detailed output from FauxPy plugin.",
        )
''',
    '''        group.addoption(
            "--fauxpy-verbose",
            action="store_true",
            default=False,
            help="Show detailed output from FauxPy plugin.",
        )
        group.addoption(
            "--mutation-selection",
            default=None,
            help="Select a framework-level MBFL mutation selection strategy.",
        )
        group.addoption(
            "--mutation-budget",
            default=None,
            help="Maximum number of mutants to validate for selected MBFL strategies.",
        )
        group.addoption(
            "--mutation-seed",
            default="0",
            help="Random seed used by framework-level MBFL mutation selection.",
        )
''',
    "pytest MBFL selection options",
)
pytest_text = replace_once(
    pytest_text,
    '''        fauxpy_verbose_opt = pytest_config.getoption("--fauxpy-verbose")
        file_or_dir = pytest_config.getoption("file_or_dir")
''',
    '''        fauxpy_verbose_opt = pytest_config.getoption("--fauxpy-verbose")
        mutation_selection_opt = pytest_config.getoption("--mutation-selection")
        mutation_budget_opt = pytest_config.getoption("--mutation-budget")
        mutation_seed_opt = pytest_config.getoption("--mutation-seed")
        file_or_dir = pytest_config.getoption("file_or_dir")
''',
    "pytest MBFL selection option reads",
)
pytest_text = replace_once(
    pytest_text,
    '''            fauxpy_verbose_opt,
            file_or_dir,
        )
''',
    '''            fauxpy_verbose_opt,
            file_or_dir,
            mutation_selection_opt,
            mutation_budget_opt,
            mutation_seed_opt,
        )
''',
    "pytest MBFL selection constructor args",
)
write(pytest_manager_path, pytest_text)

fl_option_manager_path = package_root / "command_line" / "pytest_mode" / "fl_option_manager.py"
fl_text = read(fl_option_manager_path)
fl_text = replace_once(
    fl_text,
    '''        fauxpy_verbose_opt: bool,
        file_or_dir,
    ):
''',
    '''        fauxpy_verbose_opt: bool,
        file_or_dir,
        mutation_selection_opt: str,
        mutation_budget_opt: str,
        mutation_seed_opt: str,
    ):
''',
    "FlOptionManager MBFL selection signature",
)
fl_text = replace_once(
    fl_text,
    '''        self._file_or_dir_opt = file_or_dir
        self._path_manager = FlPathManager(
''',
    '''        self._file_or_dir_opt = file_or_dir
        self._mutation_selection = mutation_selection_opt
        self._mutation_budget = mutation_budget_opt
        self._mutation_seed = mutation_seed_opt
        self._path_manager = FlPathManager(
''',
    "FlOptionManager MBFL selection fields",
)
fl_text = replace_once(
    fl_text,
    '''                self._file_or_dir_opt,
                report_directory_path,
                Path(self._project_working_directory.get_absolute()),
            )
''',
    '''                self._file_or_dir_opt,
                report_directory_path,
                Path(self._project_working_directory.get_absolute()),
                self._mutation_selection,
                self._mutation_budget,
                self._mutation_seed,
            )
''',
    "MbflSession MBFL selection args",
)
write(fl_option_manager_path, fl_text)

mbfl_session_path = package_root / "fault_localization" / "mbfl" / "session_lib.py"
mbfl_session_text = read(mbfl_session_path)
mbfl_session_text = replace_once(
    mbfl_session_text,
    '''        file_or_dir,
        report_directory_path: Path,
        project_working_directory: Path,
    ):
''',
    '''        file_or_dir,
        report_directory_path: Path,
        project_working_directory: Path,
        mutation_selection: str = None,
        mutation_budget: str = None,
        mutation_seed: str = "0",
    ):
''',
    "MbflSession MBFL selection signature",
)
mbfl_session_text = replace_once(
    mbfl_session_text,
    '''        self._mutation_manager = MutationManager(self._db_manager, self._mutation_strategy)
''',
    '''        self._mutation_manager = MutationManager(
            self._db_manager,
            self._mutation_strategy,
            mutation_selection,
            mutation_budget,
            mutation_seed,
        )
''',
    "MutationManager MBFL selection args",
)
write(mbfl_session_path, mbfl_session_text)

mutation_manager_path = package_root / "fault_localization" / "mbfl" / "mutation_manager.py"
mutation_text = read(mutation_manager_path)
mutation_text = replace_once(
    mutation_text,
    '''import logging
from pathlib import Path
''',
    '''import logging
import random
import time
from pathlib import Path
''',
    "MutationManager imports",
)
mutation_text = replace_once(
    mutation_text,
    '''    def __init__(
            self,
            db_manager: MbflDbManager,
            mutation_strategy: MutationStrategy
    ):
''',
    '''    def __init__(
            self,
            db_manager: MbflDbManager,
            mutation_strategy: MutationStrategy,
            mutation_selection: str = None,
            mutation_budget: str = None,
            mutation_seed: str = "0",
    ):
''',
    "MutationManager signature",
)
mutation_text = replace_once(
    mutation_text,
    '''        self._db_manager = db_manager
        self._mutation_strategy = mutation_strategy
''',
    '''        self._db_manager = db_manager
        self._mutation_strategy = mutation_strategy
        self._mutation_selection = mutation_selection
        self._mutation_budget = int(mutation_budget) if mutation_budget not in [None, ""] else None
        self._mutation_seed = int(mutation_seed or 0)
''',
    "MutationManager fields",
)
cosmic_ray_path = package_root / "fault_localization" / "mbfl" / "mutation_lib" / "cosmic_ray.py"
cosmic_ray_text = read(cosmic_ray_path)
cosmic_ray_text = replace_once(
    cosmic_ray_text,
    '''    def _get_all_mutant_list(
        self, module_path: str, operator_names: List[str]
    ) -> List[Mutant]:
''',
    '''    def _get_all_mutant_list(
        self,
        module_path: str,
        operator_names: List[str],
        line_numbers: Optional[List[int]] = None,
    ) -> List[Mutant]:
''',
    "Cosmic Ray line-limited mutant list signature",
)
cosmic_ray_text = replace_once(
    cosmic_ray_text,
    '''            for occurrence, (start_pos, end_pos) in enumerate(positions):
                original_code, mutated_code = self.produce_mutation(
                    module_path, operator, occurrence
                )
''',
    '''            for occurrence, (start_pos, end_pos) in enumerate(positions):
                if (
                    line_numbers is not None
                    and start_pos[0] not in line_numbers
                    and end_pos[0] not in line_numbers
                ):
                    continue

                original_code, mutated_code = self.produce_mutation(
                    module_path, operator, occurrence
                )
''',
    "Cosmic Ray line-limited mutation production",
)
cosmic_ray_text = replace_once(
    cosmic_ray_text,
    '''        mutant_list = self._get_all_mutant_list(module_path, operator_names)
''',
    '''        mutant_list = self._get_all_mutant_list(module_path, operator_names, line_numbers)
''',
    "Cosmic Ray line-limited call",
)
write(cosmic_ray_path, cosmic_ray_text)
old_get_all = '''    def get_all_mutants_for_failing_line_number_list(
        self, failing_line_number_list
    ) -> List:
        """
         Generates mutants for the given statements. Each statement contains
          information about the module and the line number the statement belongs to.

         Args:
             failing_line_number_list (List[str]): A list of statements to generate mutants for.

         Returns:
             List[Mutant]: A list of mutants corresponding to the given statements.
         """
        mutant_list = []

        for statement_name in failing_line_number_list:
            path, line_number = naming_lib.convert_statement_name_to_components(
                statement_name
            )
            self._db_manager.insert_failing_line_number_components(path, line_number)

        failing_module_path_list = (
            self._db_manager.select_distinct_failing_module_paths()
        )
        for module_path in failing_module_path_list:
            line_number_list = self._db_manager.select_failing_line_numbers_for_module_path(
                module_path
            )

            current_module_mutant_list = self._get_module_mutant_list(module_path, line_number_list)

            mutant_list += current_module_mutant_list

        self._set_mutant_ids(mutant_list)

        return mutant_list
'''
new_get_all = '''    def get_all_mutants_for_failing_line_number_list(
        self, failing_line_number_list
    ) -> List:
        """
         Generates mutants for the given statements. Each statement contains
          information about the module and the line number the statement belongs to.

         Args:
             failing_line_number_list (List[str]): A list of statements to generate mutants for.

         Returns:
             List[Mutant]: A list of mutants corresponding to the given statements.
         """
        candidate_statement_name_list = list(failing_line_number_list)
        selected_statement_name_list = list(candidate_statement_name_list)

        if self._mutation_selection == "random":
            rng = random.Random(self._mutation_seed)
            if (
                    self._mutation_budget is not None
                    and len(selected_statement_name_list) > self._mutation_budget
            ):
                selected_statement_name_list = rng.sample(
                    selected_statement_name_list,
                    self._mutation_budget,
                )
            else:
                rng.shuffle(selected_statement_name_list)

            fl_print.normal(f"Candidate mutation locations: {len(candidate_statement_name_list)}")
            fl_print.normal(f"Selected mutation locations: {len(selected_statement_name_list)}")

        generation_start = time.perf_counter()
        mutant_list = []

        for statement_name in selected_statement_name_list:
            path, line_number = naming_lib.convert_statement_name_to_components(
                statement_name
            )
            self._db_manager.insert_failing_line_number_components(path, line_number)

        failing_module_path_list = (
            self._db_manager.select_distinct_failing_module_paths()
        )
        for module_path in failing_module_path_list:
            line_number_list = self._db_manager.select_failing_line_numbers_for_module_path(
                module_path
            )

            current_module_mutant_list = self._get_module_mutant_list(module_path, line_number_list)

            mutant_list += current_module_mutant_list

        generated_mutant_count = len(mutant_list)
        if (
                self._mutation_selection == "random"
                and self._mutation_budget is not None
                and generated_mutant_count > self._mutation_budget
        ):
            rng = random.Random(self._mutation_seed)
            mutant_list = rng.sample(mutant_list, self._mutation_budget)

        self._set_mutant_ids(mutant_list)
        generation_time = time.perf_counter() - generation_start

        fl_print.normal(f"Total generated mutants: {generated_mutant_count}")
        fl_print.normal(f"Mutants selected for validation: {len(mutant_list)}")
        fl_print.normal(f"Mutation generation time: {generation_time:.4f}")

        return mutant_list
'''
mutation_text = replace_once(
    mutation_text,
    old_get_all,
    new_get_all,
    "MutationManager selection implementation",
)
write(mutation_manager_path, mutation_text)

# Fix: FauxPy MBFL uses bare "python" for subprocess calls, but in pyenv-managed
# environments the "python" shim resolves to no version unless pyenv global is set.
# Replace with sys.executable so the subprocess inherits the venv's Python binary.
collect_mbfl_api_path = package_root / "fault_localization" / "collect_mbfl" / "api_lib.py"
api_text = read(collect_mbfl_api_path)
api_text = replace_once(
    api_text,
    '''from fauxpy.fault_localization.collect_mbfl.api_file import CollectMbflApiFileManager
from fauxpy.fault_localization.util.run_lib import CommandRunner
from fauxpy.session_lib.fauxpy_path import FauxpyPath''',
    '''import sys

from fauxpy.fault_localization.collect_mbfl.api_file import CollectMbflApiFileManager
from fauxpy.fault_localization.util.run_lib import CommandRunner
from fauxpy.session_lib.fauxpy_path import FauxpyPath''',
    "CollectMbflApi sys import",
)
api_text = replace_once(
    api_text,
    '''        command = (
            ["python", "-m", "pytest"]''',
    '''        command = (
            [sys.executable, "-m", "pytest"]''',
    "CollectMbflApi sys.executable fix",
)
write(collect_mbfl_api_path, api_text)

mbfl_run_manager_path = package_root / "fault_localization" / "mbfl" / "mbfl_run_manager.py"
run_text = read(mbfl_run_manager_path)
run_text = replace_once(
    run_text,
    '''import shutil
from enum import Enum
''',
    '''import shutil
import time
from enum import Enum
''',
    "MbflRunManager imports",
)
run_text = replace_once(
    run_text,
    '''        number_of_all_mutants = len(mutants)

        fl_print.normal(f"Running {number_of_all_mutants} Mutants")
''',
    '''        number_of_all_mutants = len(mutants)
        validation_start = time.perf_counter()

        fl_print.normal(f"Running {number_of_all_mutants} Mutants")
''',
    "MbflRunManager validation timer start",
)
run_text = replace_once(
    run_text,
    '''        self._remove_temp_project(temp_project_path)
''',
    '''        validation_time = time.perf_counter() - validation_start
        fl_print.normal(f"Mutant validation time: {validation_time:.4f}")
        self._remove_temp_project(temp_project_path)
''',
    "MbflRunManager validation timer end",
)
write(mbfl_run_manager_path, run_text)

print("FauxPy MBFL selection patch applied.")
