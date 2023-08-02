# run.codes - Compiler Engine

This project contains the compiler engine written in Python. The compiler engine is responsible for compiling 
and processing submissions by pooling over the database and updating the results directly on the database.

## Build & Run

The recommended way to run and build the project is by using Docker Compose. To use this method, you need to have Docker and Docker Compose installed. If you have any doubts on how to do it, follow the [official guide for Docker](https://docs.docker.com/engine/install/) and the [official guide for Docker Compose](https://docs.docker.com/compose/install/). Please note that you need to mount `/var/run/docker.sock` to use it through Docker.

### Configuration

The project's configuration is done through environment variables, which can be checked on the `rcc/config.py` file.

## Additional Details

The Compiler-Engine does not provide an API for external access. The entry point of the application is the 
rcc package (on the `__init__.py`). The recommended execution method is through Docker Compose, even though
pipenv is used to manage dependencies.

## License

For information on the license of this project, please see our [license file](LICENSE.md).

## Contributors

For information of the contributors of this project, please see our [contributors file](CONTRIBUTORS.md).

## Contributing

For information on contributing to this project, please see our [contribution guidelines](CONTRIBUTING.md).
