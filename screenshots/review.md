# Exception triage design review (1 round, desktop)

Screenshots: triage-results-1440.png, triage-results-1280.png, triage-drawer-INV42-1440.png, triage-drawer-GAC118-1440.png, triage-drawer-SGS0455-1440.png, triage-drawer-INV0215-1440.png

| item | score | note |
|---|---|---|
| Headline number lands first | 5 | 64px red Rs 7,11,230 in the top-left card |
| Customer's own words | 5 | Their column names (Bill Number, Party Name, GSTIN/UIN, Amount Paid) and vendor names in the drawer |
| Before and after on one screen | 4 | Original and duplicate rows side by side in the drawer, with the proving cells highlighted |
| No default template look | 4 | Restrained palette, clear type scale; plain white cards |

Verified: table headers do not overlap at 1440 or 1280 (text fits each th, no horizontal scroll); no ellipsis or line clamp anywhere (the only ellipsis hits are the hidden file inputs); highlights land on Rate/Unit Price (GAC/118), Amount Paid/Invoice Amount (SGS/0455), GSTIN (INV-0215).

Fix this round: the INV/42 Invoice Amount proof cell was cut off at the drawer edge at 1440. Drawer widened 860 to 1000px and mini table cell padding 10 to 8px; it now sits fully on screen. Remaining columns (Date onward) still scroll, with edge shadow and the "Scroll sideways" cue.
