# Textile POS (single counter)
1. Install Python 3.10+.
2. The shop name and address are configured in main.py. Replace the included shop icon by updating baby-murugan-images-5.png.
3. Windows: double-click start.bat. Mac/Linux: sh start.sh. Open http://localhost:8080
4. FIRST: unlock any tab marked with a lock using the default owner PIN 1234, then use "Change owner PIN".
5. Add items in the Items tab. To receive stock, open Stock in, search the catalog and select each item, enter supplier and supplier bill details, then enter the received quantity and unit cost for each line before saving. Billing works by typing item names in English or Tamil; use "Add one-off item" for a sale that is not in the catalog.
6. Backup nightly: python backup.py "D:\path\to\cloud-folder"  (data is in shop.db)

One-off bill lines use a manually entered name, unit, GST-inclusive price, and GST rate. For meter items, enter the measured length and the selling price per meter including GST; the same price is used on the bill and receipt. They appear on the invoice but do not change stock or become catalog items.
Customer credit shows each open invoice number separately. Collections can be recorded against a selected invoice, and existing unassigned collections are applied to the oldest open credit invoice first.

Features: GST bills (CGST/SGST), split payments, credit (udhaar) accounts with collections, returns/credit notes,
purchases, item edit/delete, barcode labels (needs internet for the label page), day close with GST summary, owner PIN.
Prices include GST. Use the sidebar theme control to switch between light and dark mode. Money is stored in paise, quantity in thousandths.
Not included: user accounts beyond the single owner PIN, supplier payments, profit reports, e-invoicing/GST filing export.
