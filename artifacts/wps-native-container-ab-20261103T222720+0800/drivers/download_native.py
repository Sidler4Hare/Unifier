import re,time,hashlib,urllib.request,json
from pathlib import Path
p=Path(__file__).resolve().parent
html=(p/'wps-page.html').read_text()
u=re.search(r"downLoad\('(https://[^']+29183[^']+x86_64.rpm)'",html).group(1)
key=re.search(r'var secrityKey = "([^"]+)"',html).group(1)
t=str(int(time.time())); uri=urllib.request.urlparse(u).path if hasattr(urllib.request,'urlparse') else __import__('urllib.parse',fromlist=['urlparse']).urlparse(u).path
signed=u+'?t='+t+'&k='+hashlib.md5((key+uri+t).encode()).hexdigest()
f=p/'downloads'/u.rsplit('/',1)[-1]; h=hashlib.sha256(); n=0
with urllib.request.urlopen(signed,timeout=30) as r,open(str(f)+'.partial','wb') as o:
 while b:=r.read(1024*1024):o.write(b);h.update(b);n+=len(b)
Path(str(f)+'.partial').replace(f)
(p/'native-package.json').write_text(json.dumps({'source_page':'https://www.wps.cn/product/wpslinuxlog','unsigned_url':u,'file':str(f),'bytes':n,'sha256':h.hexdigest()},indent=2))
print(json.dumps({'status':'downloaded','bytes':n,'sha256':h.hexdigest()}))
