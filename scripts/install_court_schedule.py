#!/usr/bin/env python3
"""Install this user's single daily court cron entry after validation/deployment."""
import argparse,sqlite3,subprocess
from pathlib import Path
MARKER='# stories-court-daily'
ENTRY='0 7 * * * /bin/bash /srv/stories-dashboard/court-runner/deploy/run-court-daily.sh >> /srv/stories-dashboard/story-data/court-daily.log 2>&1 '+MARKER
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--db',type=Path,required=True);a=p.parse_args()
zone=subprocess.run(['timedatectl','show','-p','Timezone','--value'],text=True,capture_output=True,check=True).stdout.strip()
if zone!='Europe/Berlin':raise SystemExit('Host must use Europe/Berlin for this cron schedule')
old=subprocess.run(['crontab','-l'],text=True,capture_output=True)
if old.returncode not in {0,1}:raise SystemExit(old.stderr)
if old.returncode==1 and 'no crontab' not in old.stderr.lower():raise SystemExit(old.stderr)
lines=[l for l in old.stdout.splitlines() if MARKER not in l]
text='\n'.join(lines+[ENTRY])+'\n'
subprocess.run(['crontab','-'],input=text,text=True,check=True)
installed=subprocess.run(['crontab','-l'],text=True,capture_output=True,check=True).stdout
if installed.count(ENTRY)!=1:raise SystemExit('Court cron entry readback failed')
with sqlite3.connect(a.db) as c:
 c.execute("INSERT INTO court_schedule(singleton,enabled,timezone,hour,minute) VALUES(1,1,'Europe/Berlin',7,0) ON CONFLICT(singleton) DO UPDATE SET enabled=1,timezone='Europe/Berlin',hour=7,minute=0")
print('Installed and verified: daily 07:00 Europe/Berlin')
