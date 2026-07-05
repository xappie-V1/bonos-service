# Imagen base ligera de Python
FROM python:3.12-slim

# Evita generar archivos .pyc
ENV PYTHONDONTWRITEBYTECODE=1

# Muestra los logs inmediatamente
ENV PYTHONUNBUFFERED=1

# Carpeta de trabajo
WORKDIR /app

# Instalar dependencias del sistema necesarias para psycopg2
RUN apt-get update && \
    apt-get install -y gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copiar primero requirements para aprovechar la cache
COPY requirements.txt .

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del proyecto
COPY . .

# Puerto del servicio
EXPOSE 8004

# Ejecutar FastAPI
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]