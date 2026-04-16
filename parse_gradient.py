import csv, re
from datetime import datetime

def extract_slp(metar):
m = re.search(r'SLP(\d{3})', str(metar))
if m:
v = float(m.group(1)) / 10.0
return 1000.0 + v if v < 50.0 else 900.0 + v
return None

def extract_pkwnd(metar):
m = re.search(r'PK WND \d{3}(\d{2,3})/', str(metar))
return int(m.group(1)) if m else 0

def parse_file(path):
data = {}
with open(path, 'r') as f:
for row in csv.DictReader(f):
try:
dt = datetime.strptime(row['valid'], '%Y-%m-%d %H:%M')
dt_hr = dt.replace(minute=0, second=0)
slp = extract_slp(row['metar'])
pkwnd = extract_pkwnd(row['metar'])
if dt_hr not in data or pkwnd > data[dt_hr].get('pkwnd', 0):
data[dt_hr] = {'slp': slp, 'pkwnd': pkwnd}
except: pass
return data

hln = parse_file('/home/progged-ish/nws_dashboard/HLN_60day_metars.csv')
gtf = parse_file('/home/progged-ish/nws_dashboard/GTF_60day_metars.csv')

cat_35 = []
cat_50 = []

for dt in sorted(gtf.keys()):
if dt in hln and gtf[dt]['slp'] and hln[dt]['slp']:
wnd = gtf[dt]['pkwnd']
if wnd >= 35:
grad = hln[dt]['slp'] - gtf[dt]['slp']
line = f"{dt} | HLN: {hln[dt]['slp']:.1f}mb | GTF: {gtf[dt]['slp']:.1f}mb | Gradient: {grad:+.1f}mb | GTF PK WND: {wnd}KT"
if wnd >= 50:
cat_50.append(line)
else:
cat_35.append(line)

with open('/home/progged-ish/nws_dashboard/wind_study_results.txt', 'w') as f:
f.write("=== 50+ KT WIND EVENTS ===\n")
if len(cat_50) > 0:
f.write("\n".join(cat_50))
else:
f.write("No 50KT events found.")
f.write("\n\n=== 35-49 KT WIND EVENTS ===\n")
if len(cat_35) > 0:
f.write("\n".join(cat_35))
else:
f.write("No 35KT events found.")

print(f"Analysis complete. Found {len(cat_50)} 50KT+ events and {len(cat_35)} 35-49KT events.")
