"""Configuração do Gunicorn (tudo via variáveis de ambiente)."""
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = 2
timeout = 30
graceful_timeout = 20
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
# Não registra query string completa nem corpos de requisição.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss'
forwarded_allow_ips = "*"
limit_request_line = 4094
