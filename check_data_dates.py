import gzip
import csv
from datetime import datetime

file_path = 'data/history/BTCUSDT_15m.csv.gz'

with gzip.open(file_path, 'rt') as f:
    reader = csv.reader(f)
    rows = list(reader)
    
print(f"Total de linhas: {len(rows)}")
print(f"\nPrimeiras 3 linhas:")
for i in range(min(3, len(rows))):
    print(rows[i])

print(f"\nÚltimas 3 linhas:")
for i in range(max(0, len(rows)-3), len(rows)):
    print(rows[i])
