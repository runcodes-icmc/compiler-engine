FROM python:3.13-trixie AS runtime


# System upgrade
RUN apt-get update -y
RUN apt-get upgrade -y

# Install dependencies
RUN apt-get install -y curl iptables libdevmapper-dev libpq-dev python3-dev

# Clean Up
RUN apt-get clean
RUN rm -rf /var/lib/apt/lists/*

FROM runtime AS build

# Upgrade PIP
RUN pip install --upgrade pip

# Install Pipenv
RUN pip install pipenv

# Setup install dir
RUN mkdir -p /app
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
CMD python main.py --config env
