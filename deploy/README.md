# VPS deploy (systemd)

## Ubuntu 22.04

1. Установите Python 3.11 и git.
2. Скопируйте проект на VPS в `/home/tradingbot/app`.
3. Проверьте каталог `config/` и заполните `.env` (`chmod 600`) с отдельными
   `TG_PREPARE_CHAT_ID` и `TG_ENTRY_CHAT_ID`.
4. Запустите:

```bash
chmod +x deploy/install.sh
./deploy/install.sh
```

## Операции

- Статус: `sudo systemctl status tradingbot`
- Перезапуск: `sudo systemctl restart tradingbot`
- Логи: `sudo journalctl -u tradingbot -f`
- Ротация: `sudo journalctl --vacuum-time=14d`
