FROM python:3.14-trixie AS runtime

# Install dependencies
RUN apt-get update &&\
    apt-get install --no-install-recommends -y curl iptables libdevmapper-dev libpq-dev python3-dev &&\
    apt-get clean &&\
    rm -rf /var/lib/apt/lists/*

FROM runtime AS build

# Install Pipenv & Setup install dir
RUN pip install --no-cache-dir pipenv &&\
    mkdir -p /app

WORKDIR /app

# Load dependencies
COPY ./Pipfile /app/Pipfile
COPY ./Pipfile.lock /app/Pipfile.lock
RUN pipenv install --system --deploy

FROM build AS dist

# Load source
COPY ./src ./

# Entrypoint
STOPSIGNAL SIGINT
CMD [ "python", "main.py", "--config", "env" ]
