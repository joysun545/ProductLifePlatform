"""Deployment verification: isolated SQLite; never uses the business database."""
from config.settings import *
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}
CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'
ALLOWED_HOSTS=['testserver','localhost','joytest.site']
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
