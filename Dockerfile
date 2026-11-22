FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent files are bind-mounted per service in docker-compose.yml.
CMD ["python", "bot.py"]
