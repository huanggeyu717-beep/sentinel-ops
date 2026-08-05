FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY apps/device-sim/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY apps/device-sim/ ./

CMD ["python", "sim.py", "scenarios/basic_spill.yaml", "--base-url", "http://api:8000", "--speed", "5"]
