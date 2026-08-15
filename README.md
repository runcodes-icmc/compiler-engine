# run.codes - Compiler Engine

This project contains the compiler engine written in Python. The compiler engine is responsible for compiling
and processing submissions by pooling over the database and updating the results directly on the database.

## Build & Run

The recommended way to run and build the project is by using Docker Compose. To use this method, you need to have Docker and Docker Compose installed. If you have any doubts on how to do it, follow the [official guide for Docker](https://docs.docker.com/engine/install/) and the [official guide for Docker Compose](https://docs.docker.com/compose/install/). Please note that you need to mount `/var/run/docker.sock` to use it through Docker.

### Configuration

The project's configuration is done through environment variables, which can be checked on the `rcc/config.py` file.

### Parallelism tuning

The engine processes commits through two nested knobs:

- `RUNCODES_COMPILER_NUM_WORKERS` (default `2`): the number of multiprocessing
  worker processes. Each worker owns its own event loop and its own database
  connection pool.
- `RUNCODES_COMPILER_CONCURRENCY` (default `4`): the number of commits each
  worker processes concurrently.

The total number of in-flight commits is the product of the two
(`num_workers × concurrency`, default 2×4 = 8). The same values are read from
JSON configuration files through the `num_workers` and `concurrency_per_worker`
keys (see `config/rcc/config.json.example`).

The workload is IO-bound (containers, S3, database), so sizing has nothing to
do with the CPU count: the real ceiling is how many compilation containers
the Docker host can run at once, plus available RAM. Worker processes are the
expensive part of the pipeline — each adds an interpreter copy, an event loop
and a database connection pool — so when the host can take more in-flight
work, prefer raising the concurrency before adding processes.

On startup the engine validates the values (`num_workers >= 1`,
`concurrency >= 1`, and a bounded task queue at least as large as the total
number of in-flight slots), refuses to start on nonsensical values and logs
one line with the effective parallelism (e.g. `workers=2, concurrency=4,
max_in_flight=8`).

### Database pool sizing

Every process (the main poller and each worker) owns its own `psycopg_pool`
connection pool, tuned through:

- `RUNCODES_DB_POOL_MIN_SIZE` (default `1`)
- `RUNCODES_DB_POOL_MAX_SIZE` — when not set, derived from the per-process
  concurrency as `concurrency + 2` (clamped to at least the minimum size); an
  explicitly configured value always wins
- `RUNCODES_DB_POOL_TIMEOUT` (default `30` seconds)

One pooled connection per in-flight commit is enough because a commit only
holds a connection for short DB bursts (a single transaction per provider
call); the `+2` margin covers transient overlap between a finishing commit
and the next one starting.

## Additional Details

The Compiler-Engine does not provide an API for external access. The entry point of the application is the
rcc package (on the `__init__.py`). The recommended execution method is through Docker Compose, even though
uv is used to manage dependencies.

## License

For information on the license of this project, please see our [license file](LICENSE.md).

## Contributors

For information of the contributors of this project, please see our [contributors file](CONTRIBUTORS.md).

## Contributing

For information on contributing to this project, please see our [contribution guidelines](CONTRIBUTING.md).
