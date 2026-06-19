FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
RUN addgroup --system appgroup && adduser --system appuser && chown -R appuser:appgroup /app
USER appuser
ENV PORT=8501
EXPOSE 8501
CMD ["sh", "start.sh"]
