import re

html = """
<tr>
    <td><input type="checkbox"></td>
    <td>&nbsp;044-2025-006161&nbsp;</td>
    <td>UCC-1</td>
    <td>&nbsp;JOHN DOE LLC&nbsp;</td>
    <td>&nbsp;05/11/2025 08:30:00 AM&nbsp;</td>
    <td>&nbsp;&nbsp;</td>
</tr>
"""

rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
for row in rows:
    cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
    print("Cells:", cells)
    clean = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', c)).strip() for c in cells]
    print("Clean:", clean)
    
    file_num    = re.sub(r'&[a-zA-Z]+;', '', clean[1]).strip()
    debtor_name = re.sub(r'&[a-zA-Z]+;', '', clean[3]).strip()
    date_raw    = re.sub(r'&[a-zA-Z]+;', '', clean[4]).strip()
    date_filed  = date_raw.split()[0] if date_raw else ""
    
    print("File:", file_num)
    print("Debtor:", debtor_name)
    print("Date filed:", date_filed)

