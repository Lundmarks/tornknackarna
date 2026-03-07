FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist player_map.json and state.json across container restarts
VOLUME ["/app/player_map.json", "/app/state.json"]

CMD ["python", "bot.py"]
