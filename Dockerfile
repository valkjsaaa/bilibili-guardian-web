FROM ubuntu:22.04

RUN apt-get update -y && \
    apt-get install -y python3-pip python3-dev git

WORKDIR /app

# Install specific versions of packages
RUN pip install werkzeug==2.0.3
RUN pip install aiohttp==3.8.5
RUN pip install --upgrade pip setuptools wheel

# Clone and install bilibili-api directly
RUN git clone https://github.com/Nemo2011/bilibili-api.git /tmp/bilibili-api && \
    cd /tmp/bilibili-api && \
    pip install -e .

# Copy project files
COPY . /app

# Create data directory
RUN mkdir -p /data/

# Install other requirements
RUN pip install -r requirements.txt

ENTRYPOINT [ "python3", "-u", "app.py", "--db", "sqlite:////data/db.sqlite?check_same_thread=False" ]

