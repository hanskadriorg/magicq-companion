#!/bin/bash
# Same as install-daily-reboot.sh: write boot + scheduled-reboot crontab from config.
exec "$(cd "$(dirname "$0")" && pwd)/install-daily-reboot.sh"
