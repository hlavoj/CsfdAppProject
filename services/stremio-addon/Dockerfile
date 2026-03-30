FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 7000
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:7000", "--workers", "4", "--timeout", "120"]
