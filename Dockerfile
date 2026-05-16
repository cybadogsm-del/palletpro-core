FROM python:3.11-slim

RUN useradd --create-home --shell /bin/bash palletpro

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && chown palletpro:palletpro /data

USER palletpro

ENV PALLET_PRO_DB=/data/pallet_pro.db

EXPOSE 8000

CMD ["gunicorn", "wsgi:app", "--config", "gunicorn.conf.py"]
