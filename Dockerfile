FROM ubuntu:22.04
RUN apt update
RUN apt install python3.10 python3-pip python3.10-dev -y
COPY . ./app
WORKDIR ./app
RUN pip install -r requirements.txt
RUN rm files/wallets.txt files/proxy.txt docker-compose.yml files/config.py
RUN rm -rf .git .idea __pycache__
ENTRYPOINT ["python3","main.py"]