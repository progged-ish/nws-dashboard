import urllib.request
from datetime import datetime, timedeltaend = datetime.now()
start = end - timedelta(days=60)
stations = ["GTF", "HLN"]
for station in stations:
    url = f'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={station}&data=metar&year1={start.year}&month1={start.month}&day1={start.day}&year2={end.year}&month2={end.month}&day2={end.day}&tz=Etc/UTC&format=onlycomma&missing=M'
    req = urllib.request.Request(url, headers={'User-Agent': 'Python-Wind-Study'})
    with urllib.request.urlopen(req) as response:
        data = response.read().decode('utf-8')
    with open(f'/home/progged-ish/nws_dashboard/{station}_60day_metars.csv', "w") as f:
        f.write(data)
