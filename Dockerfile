FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sigenergy_modbus_mqtt.py config.yaml ./
CMD ["python","sigenergy_modbus_mqtt.py","-c","config.yaml"]
