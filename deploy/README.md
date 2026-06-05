# VPS deploy (systemd)

## Ubuntu 22.04

1. Установите Python 3.11 и git.
2. Скопируйте проект на VPS в `/home/tradingbot/app`.
3. Создайте локальный конфиг: `cp config.example.yaml config.yaml`.
4. Заполните `.env` (chmod 600).
5. Запустите:

```bash
chmod +x deploy/install.sh
./deploy/install.sh
```

## Операции

- Статус: `sudo systemctl status tradingbot`
- Перезапуск: `sudo systemctl restart tradingbot`
- Логи: `sudo journalctl -u tradingbot -f`
- Ротация: `sudo journalctl --vacuum-time=14d`
