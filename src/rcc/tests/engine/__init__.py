import rcc.config

rcc.config.from_dict(
    rcc.config.DEFAULT_CONFIG,
    {
        "exec_dir": "/var/runcodes/runs",
        "src_dir": "src",
        "output_files_dir": "outputfiles",
        "compilation_error_file": "compilation.err",
        "compilation_output_file": "compilation.out",
        "compilation_timeout": 10,
        "base_exec_timeout": 5,
        "monitor_max_file_size": 5242880,
        "monitor_max_mem_size": 268435456,
        "container_cfg_file": "container.config",
    },
)
