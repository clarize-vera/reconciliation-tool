FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create directory for Flask sessions
RUN mkdir -p /app/flask_session

# Run as non-root user
RUN useradd -m appuser
RUN chown -R appuser:appuser /app
USER appuser

# Run the application
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 app:app
