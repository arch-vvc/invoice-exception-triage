"""Regenerate the invoices_*.csv variants from invoices_baseline.csv: python3 tests/fixtures/messy/make_variants.py

The variants imitate how accounting exports differ. They are written from general knowledge, not from a real export.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
base = (HERE / "invoices_baseline.csv").read_text(encoding="utf-8")
lines = base.splitlines()


def write(name: str, text: str, encoding: str = "utf-8") -> None:
    (HERE / name).write_bytes(text.encode(encoding))


write("invoices_aliases.csv",
      base.replace("Date,Bill Number", "Invoice Date,Voucher No").replace("Invoice Amount", "Gross Total"))
write("invoices_dash_dates.csv", base.replace("/04/2024", "-04-2024"))
write("invoices_mon_dates.csv", base.replace("/04/2024", "/Apr/24"))
write("invoices_rupee.csv", base.replace('"5,900.00"', '"₹5,900.00"').replace('"23,600.00"', '"₹23,600.00"')
      .replace('"1,23,456.00"\n', '"₹1,23,456.00"\n'))
write("invoices_bom.csv", base, "utf-8-sig")
write("invoices_cp1252.csv", base.replace("Box,", "Café box,").replace('"5,900.00"', '"Rs. 5,900.00"'), "cp1252")
write("invoices_blank_lines.csv",
      "\n\n" + lines[0] + "\n\n" + lines[1] + "\n" + lines[2] + "\n\n" + lines[3] + "\n\n\n")
write("invoices_grand_total.csv", base + 'Grand Total,,,,,,,,,,,"1,52,956.00"\n')
write("invoices_missing_column.csv",
      "Date,Bill Number,Party Name,GSTIN/UIN,Order No,Particulars,Billed Qty,Rate,Taxable Value,Tax Amount\n"
      '10/04/2024,KP/001,Kumar Packaging Pvt Ltd,24AABCK4417M1ZJ,PO-1,Box,100,50.00,"5,000.00",900.00\n')
