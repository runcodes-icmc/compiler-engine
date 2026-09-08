import rcc.config

# Configuration for the docker-backed engine tests. The engine now takes its
# config explicitly, so tests build their own copy from this instead of
# relying on a global registry.
TEST_CONFIG = rcc.config.Config(
    {
        "exec_dir": "/var/runcodes/runs",
        "exec_dir_remote": "/var/runcodes/runs",
        "src_dir": "src",
        "output_files_dir": "outputfiles",
        "compilation_error_file": "compilation.err",
        "compilation_output_file": "compilation.out",
        "compilation_timeout": 10,
        "base_exec_timeout": 5,
        "monitor_max_file_size": 5242880,
        "monitor_max_mem_size": 268435456,
        "container_cfg_file": "container.config",
        "max_output_file_size": 1048576,
        "cleanup_on_error": False,
    },
)
