# Security

> **ВАЖНО: АВТОР НЕ ПОДДЕРЖИВАЕТ И НЕ ОДОБРЯЕТ ИСПОЛЬЗОВАНИЕ АВТОМАТИЗИРОВАННЫХ СРЕДСТВ ДЛЯ ВОЗДЕЙСТВИЯ НА РАБОТУ САЙТОВ ПОСОЛЬСТВ, КОНСУЛЬСТВ И ВИЗОВЫХ СИСТЕМ ЛЮБЫХ СТРАН, ВКЛЮЧАЯ США. ПУБЛИКАЦИЯ ПРОЕКТА НЕ ЯВЛЯЕТСЯ ПРИЗЫВОМ К ТАКОМУ ИСПОЛЬЗОВАНИЮ. СОБЛЮДАЙТЕ ПРАВИЛА И ОГРАНИЧЕНИЯ СООТВЕТСТВУЮЩИХ СЕРВИСОВ.**

This application is intended for local use on a trusted computer. It stores AIS credentials, session cookies, account settings and diagnostic artifacts under `.env` and `state/`. Application-level encryption of AIS credentials is not implemented. Keep these files private and protect backups.

Never attach a live database, environment file, browser profile, authentication headers or unredacted debug artifacts to a public issue. Use synthetic examples.

To report a vulnerability, use this repository's **Security > Report a vulnerability** private reporting channel when available. If private reporting is unavailable, open an issue asking the maintainer to enable it without describing the vulnerability or including sensitive data. The maintainer of this repository is the security contact; there is no guaranteed response time.

The repository contains no production credentials, deployment configuration, notification destination or existing user database. Use your own account and endpoints. Automatic booking can change a real appointment when priority dates are enabled.
