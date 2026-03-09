FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY i3x_grafana_adapter.py .

EXPOSE 9000

CMD ["python", "i3x_grafana_adapter.py"]
