FROM ubuntu:20.04

RUN apt-get update -y && \
    apt-get install -y python3-pip python3-dev git

# We copy just the requirements.txt first to leverage Docker cache
COPY ./requirements.txt /app/requirements.txt

WORKDIR /app

RUN pip install -r requirements.txt

COPY . /app

RUN mkdir /data/

ENTRYPOINT [ "python3", "-u", "app.py", "--db", "sqlite:////data/db.sqlite?check_same_thread=False" ]

