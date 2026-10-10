# ADMI FAI Studio

A desktop app for AS9102 First Article Inspection reports (Forms 1, 2 and 3) and dimensional inspection reports. It's written in Python (PySide6) and stores everything in one shared **PostgreSQL** database. You can install it on any number of workstations. When one person edits a report, everyone else who has that report open sees the change **live** (PostgreSQL LISTEN/NOTIFY).

## What it does

| Area | Features |
|---|---|
| **Inputs** | Drawings: PDF, multi-page TIFF, JPEG, PNG. Photos: JPEG, TIFF, PNG, BMP. Parts lists: XLSX, CSV, TSV, TXT, DOCX tables, PDF tables. Notes lists: DOCX, TXT, PDF, XLSX, CSV. CMM or Excel measured-results files. You can drag and drop files onto a report. |
| **Auto-build** | The New Report wizard uploads the drawing and finds the dimension callouts (**auto-balloon**). It then reads the drawing NOTES into Form 3, finds referenced specs (AMS, MIL, ASTM, QQ, BAC…) for Form 2, and maps the parts list into the Form 1 index. You review each step before anything is added. |
| **Balloon a part number** | Upload a drawing (PDF, TIFF, or a whole drawing-package ZIP), and the app reads it: title block (drawing no., rev, CAGE, title), **title-block tolerances**, the **parts list on the drawing** (find no., part no., nomenclature, sheet/zone, qty-per-assembly columns, flag notes), **tabulated dash tables**, and every **view title** ("003 THREADED ARM", "-501 KIT", "SECTION A-A", "DETAIL B"). You then **pick which part number you're ballooning** and it balloons **only that part's views** — including its sections/details (followed by their sheet/zone flags or cutting-plane letters), or the parent assembly view when a detail has no view of its own. Tabulated drawings get the values from that dash's row. Only the notes that apply to that part are proposed (general notes + its flag notes / coded notes marked for its dash). Material goes to Form 2 and, for assemblies, the child parts go to the Form 1 index. |
| **Auto-balloon details** | Form 3 gets the sheet/zone reference location, `CC` critical-characteristic flags as designators, stacked tolerances (`.100 +.000/-.030`), stacked limits, counts (`4X`), BASIC boxed dims (added as N/A), reference dims (unchecked), GD&T frames, and threads with their modifiers (`MIN 4 FULL THREADS`). **⬚ Balloon Area** lets you drag a box around any view to balloon just that. When you balloon the **entire drawing**, a whole sheet or an area, each characteristic's Comments show the part number of the view the dimension is shown on (e.g. `985AB1234-308`). Scanned drawings and outlined-text PDFs are read by built-in OCR (RapidOCR, pip-installed — no admin rights), and those results are marked "OCR – verify". The reader **learns from every correction** made to an OCR read (see OCR learning). |
| **Ballooning** | Click on the drawing to add a balloon. Drag balloons to move them. Click a balloon to select its row, and the reverse. You can renumber by position. Balloon color shows status: orange = open, green = accept, red = reject. |
| **Requirements** | Type the callout the way it appears on the drawing. The app works out the nominal, tolerance and LSL/USL. It handles `1.250 ±.005`, `Ø.250 +.002/-.000`, `1.245-1.255`, `.010 MAX`, `R.125 MIN`, `4X`, angles, BASIC/REF, GD&T frames such as `⌖ Ø.014 Ⓜ A B C`, and title-block default tolerances by decimal places. |
| **Results** | You can enter several samples (`1.2502, 1.2497`). Accept/Reject is set automatically and out-of-tolerance values turn red. Pressing Enter moves to the next row. Text results like `OK` or `CONFORMS` are accepted. Results can also be imported from a CMM or Excel file. Inspection and Standard reports can cover **several parts** (a column per part, optional low & high readings per part, serial number per part, per-part accept/reject). |
| **Live editing** | Every field can be edited on every tab, including after the report is generated. Changes save as you type and show up on other workstations within a second. If two people change the *same* field, the second person is warned instead of silently overwriting the first. |
| **Workflow** | Draft → In Review → Approved (locked on every workstation) → Reopen (you must give a reason). The completeness check flags blank results, rejects without an NCR number, duplicate numbers and expired gage calibrations. |
| **Outputs** | A PDF with AS9102 Rev C-style Forms 1/2/3 (or an inspection report), ballooned drawing pages that keep the original vector PDF, a photo appendix, optional appended certs, and page numbering. An Excel export is also available. There is a live preview inside the app. |
| **Traceability** | A full audit trail (who, which workstation, old → new value), a gage library with calibration due dates, and "New Revision / Copy" for delta (partial) FAIs. |

## 1. Set up the database server (once)

Use any machine that is always on, such as a server or the QC PC, with **PostgreSQL 13 or later** (16 recommended).

1. Install PostgreSQL from https://www.postgresql.org/download/.
2. Edit `server_setup/01_create_database.sql` to set real passwords, then run:
   `psql -U postgres -f server_setup/01_create_database.sql`
3. Allow the other workstations to connect:
   * `postgresql.conf` → `listen_addresses = '*'`
   * `pg_hba.conf` → add your shop LAN, for example:
     `host  fai_studio  fai_app  192.168.1.0/24  scram-sha-256`
   * Restart the PostgreSQL service. Open TCP port **5432** in the server's firewall, for the LAN only.
4. Schedule `server_setup/backup_fai_studio.bat` to run nightly (Task Scheduler). Drawings and photos are stored inside the database, so this one backup covers everything.

> **CUI / ITAR:** drawings are stored in the database. Put the server inside your CUI enclave, set SSL mode to `require`, and restrict `pg_hba.conf` to enclave workstations only.

## 2. Install on each workstation

**Windows (Python):** install Python 3.10+ (tick *Add to PATH*), then double-click **`install_windows.bat`**. This creates a desktop shortcut.
**Windows (no Python on the PCs):** on one PC, run `install_windows.bat` and then `build_exe_windows.bat`. Copy `dist\FAI Studio\` to the other PCs or to a network share.
**Linux / macOS:** run `./run_linux_mac.sh`.

The first time it starts, the app asks for the server host, database (`fai_studio`), user (`fai_app`), password and **your name**. Your name is what gets recorded on reports and in the history. The password is kept in the Windows Credential Manager. The first workstation to connect creates the tables automatically.

### Working offline (check-out / check-in)

Use this on a laptop at the machine, at a customer or at a source inspection: anywhere the server can't be reached.

- **💼 Take Offline:** select a report and it's copied to this PC with its drawings, balloons, photos and results. Every other workstation sees it as read-only (*🔒 Offline on LAPTOP-2 (Tim)*) until it comes back. Approved reports aren't taken offline.
- **Work offline:** everything works on the offline copy: ballooning (including OCR), results, photos, notes, and PDF / Excel export. The copy is marked with a yellow *Offline copy on this PC* banner.
- **New reports offline:** create them as normal while offline. They get a temporary number (e.g. *IR-TMP-LAPTOP2-001*) and receive their real FAI-/IR- number when they sync.
- **Starting without the server:** if the server can't be reached when the app starts, choose **💼 Work Offline**. **🔌 Go Online / Go Offline** switches at any time.
- **⇅ Sync:**
  - Each offline report is uploaded in **one all-or-nothing step**: the header, parts list, materials, characteristics, and new, changed or deleted attachments. The offline history goes into the audit trail with the **times it really happened**, marked *(offline)*.
  - The report is then checked back in, and the copy on the laptop is **deleted**.
  - Tick *keep working offline* to upload but keep the report on the laptop.
  - Coming back online, the app offers to sync automatically.
- **🔓 Release Check-out (admin):** frees a report whose laptop is lost or can't sync. If that laptop syncs later, nothing is overwritten: its offline work arrives as a separate report labelled *OFFLINE COPY of …* for someone to compare.
- **Online-only actions:** Approve & Lock, Reopen, and editing the gage library or company settings. The last company settings, logo and gage list are kept on the PC so reports print correctly offline.
- **CUI / ITAR:** offline copies contain export-controlled drawings. Offline PCs must be inside your CUI boundary, with BitLocker (full-disk encryption) on, and listed in the SSP. Only checked-out and new reports are ever stored, in `%APPDATA%\ADMI_FAI_Studio\offline_reports.sqlite3`, and they're removed from the PC as soon as they sync or are discarded.

### Report types

| Type | What you get |
|---|---|
| **AS9102 FAI** | Forms 1, 2 and 3 in the ADMI layout: company banner with logo, shaded rows, colour-coded Accept/Reject. |
| **AS9102 FAI – Clean** | Forms 1, 2 and 3 exactly as the plain AS9102C form set: every field is a grey label cell over a white value cell, black and white, the Rev C field names (*4. FAIR Identifier*, *17. Part Type*, *20. FAIR Verified By*, …), no logo, only "Page x of y" at the bottom. Use it for customers who want the standard forms. Form 1 field 17 *Part Type* is filled from the Notes column of the Form 1 index. |
| **Standard Report** | One inspection report laid out like AS9102 Form 3 (same cells and columns, plus Accept/Reject), with a part / drawing / PO / job header, a results summary, the title-block tolerances and an inspected-by / approved-by sign-off. The company logo appears in the title row. |
| **Inspection Report** | Dimensional report with nominal, tolerance, LSL / USL, actual and status columns, company banner with logo. |

Change the type any time on Form 1 (*Report type*). The same data prints in every layout.

**Exporting each document on its own:** on **Preview & Export**, the **Document** list picks what to preview and export: any of the four report types above, or the **Certificate of Conformance**. The type set on Form 1 is only the default, so one report can be exported as a clean AS9102 for one customer and a Standard Report for another. **📦 Export Several…** saves any combination in one go, each as its own PDF (plus an optional Excel copy of each inspection report), named like `FAI-2609-0165_99X1234-3_FAI_Clean.pdf` and `99X1234-3_COC.pdf`.

### Certificate of Conformance (CoC)

The **Certificate of Conformance** tab builds the CoC in the same layout as ADMI's paper form. Every page carries the company header and the footer statement. The pages hold the part information, the certification statement, the Material / Process / Test certifications table (Item | Type | Vendor Name / Info | Cert / Lot #) and the certified-by block.

1. Upload the drawing and whatever lists you have (parts list / BOM / CEBOM / notes list / application list; PDF, text or scanned). Then click **⚙ Build from drawing / BOM / notes**.
2. The app works out **what the part is made of and what was done to it, for exactly the part number on Form 1 (including the dash number)**:
   - **Detail part:** only its own raw material. That includes the forging, bar or extrusion it is *made from* (e.g. `MAKE FROM 99X1234-5` → the forging with its material and spec). It also gets the outside processing and tests that apply to that dash number (anodize, prime / paint, heat treat, passivate, penetrant, ultrasonic …).
   - **Assembly:** walks down through every detail and sub-assembly below it. It lists each detail's material, the purchased hardware (with quantities from the list), and the processes of every level, including assembly-level ones such as touch-up and sealing. When several details share a process, it prints once with all their part numbers (`99X5678-9, -13, -15, -23 / PRIME AND DN9907 …`).
   - Notes that are not certifications are left out, e.g. tolerancing, *NOT REQUIRED*, *NO ORGANIC COATING*, marking, packaging, installation, workmanship and weight limits.
   - **Every source of technical data is used:**
     - drawings and the parts lists on them, separate parts lists / BOMs / EPLs;
     - notes lists (PDF, scanned, Word, Excel or text) and the Form 3 note rows;
     - **engineering orders** (EO / AEO, OCR'd when scanned), **specifications**, and **other** technical data attached to the report.
   - From all of these it takes every **raw material** (including *MATERIAL:* notes and extrusions / bar / forgings a detail is machined from), every **purchased / COTS** part, every **special or outside process** and every **test / inspection**.
   - Each item appears once, even when the parts list, a note and Form 2 all call it out.
   - Engineering-order and specification call-outs are only taken when they name a specification, and their source is shown so they can be checked.
3. Vendors come from the **📇 Vendor Library** (⚙ Settings menu, shared by all PCs). They are matched by CAGE code, McMaster-Carr part number, the parts-list supplier line, or the Lockheed vendor data (with alternates). Pick a vendor in the Vendor column and its address fills in.
4. Fill in **Cert / Lot #** (e.g. `PO 1214 / LOT# 9208145`), pack list, quantity and date shipped, then **⬇ Export CoC PDF**. It warns you if any rows have no cert or lot number.

Documents it reads:

| Document | What is used |
|---|---|
| Boeing PDM part list (text) | QTY per dash column (`*` = the dash itself, a number = used in it), make-from, stock size, material, spec, supplier; the NOTES pages with applicability per dash; the APPLICATION table |
| Boeing / Douglas notes list (NL), text or scanned | note code, text and applicability marks (`*` `P` `A` `B`) per dash column. On scans the marks are found from the ink in each column, and sideways scans are turned upright. |
| Boeing application list (AL) | configuration part → next assembly |
| Old Douglas parts list (scanned) | dash rows, quantities, stock / material / spec, reference process list. Two sheets scanned side by side on one page are read separately. A row whose part number the scan lost is recovered from its `*` mark, and OCR slips such as `DPS 4.3O1`, `METH0D` and `TREATEO` are corrected. |
| Engineering orders, specifications, other documents (PDF, scanned, Word, text) | material / process / test call-outs that name a specification |
| Lockheed Engineering Parts List / CEBOM (text) | structure (raw stock "as required", consumables, hardware), BOM documents (processes / tests), material stock size, PMP specifications, vendor data |
| Lockheed Engineering BOM with Associated Data (scanned) | the same tables, read with grid detection + OCR |
| Drawings with a parts list (Lockheed 985 series etc.) | parts list, flag notes per detail (as before) |

Separate lists for the details of an assembly (one EPL per part number) are joined automatically. Scanned lists take 10–60 s per page the first time and are cached after that. OCR can misread small text, so check the rows before signing.

The certification statement, the footer and the default signer title are set in **⚙ Settings → Company, Logo & CoC Text**.

### Copy and paste in the tables

Form 3, and the Form 1, Form 2, Attachments and CoC tables, work like a spreadsheet:

| Keys | What it does |
|---|---|
| **Ctrl+C** | Copy the selected cells (tab-separated, so they paste into Excel or Word too) |
| **Ctrl+V** | One copied value → pastes into **every selected cell** (e.g. copy a gage, select 20 rows of the Gage column, paste). A copied block (from FAI Studio or Excel) → pastes from the current cell down and to the right. |
| **Ctrl+D** | Fill down: the first selected cell's value into the selected cells below it |
| **Delete** | Clear the selected cells |
| **Ctrl+Z** | Undo the last paste / fill / clear (the last 20 are kept) |

- The same commands are on the right-click menu.
- Rows hidden by Search / Show are skipped.
- Read-only columns, and values that aren't a valid choice for a drop-down column (e.g. Accept / Reject / N/A), are left alone. The status bar says how many cells were pasted or skipped.
- Each pasted cell is saved like a typed one, so limits and Accept / Reject recalculate. Other workstations see the change, and History records it.
- **Shift+click** or **Ctrl+click** selects cells; click a column's cells and drag to select a run.

### Gages on Form 3

The **Tooling / Gage** column is a drop-down of the **Gage Library** (⚙ Settings → Gage Library, shared by all workstations).
- Click the cell to see every active gage with its description, type and calibration due date. Overdue gages are marked **⚠ OVERDUE**.
- **Start typing any part of the ID, description or type** (e.g. `mic`, `height`) to find a gage.
- The cell stores the gage ID. A gage typed that isn't in the library is kept, but shown in orange. A gage whose calibration is overdue is shown in red.
- Gages added or changed on any workstation show up the next time you open the drop-down.
- **🛠 Tools → Set tooling / gage…** uses the same finder to set the gage on several selected rows at once.

### Company logo and application icon

Go to **⚙ Settings → 🏢 Company, Logo & CoC Text → Branding**. The settings are shared by every workstation.
- **Company logo:** printed on the AS9102 FAI, Standard Report and Inspection Report, and never on the AS9102 FAI – Clean forms. A PNG with a transparent background works best.
- **Application icon:** used for the window and taskbar on every PC, and on Windows also for the "FAI Studio" desktop shortcut. It takes effect on each PC the next time the app starts. Use a square PNG or .ico, 256 × 256 px or larger.

### Customer-ballooned drawings (SpaceX and similar)

Some customers put their own characteristic numbers on the print. SpaceX, for example, puts a small diamond or hexagon with a number next to every inspectable element. When a drawing like that is uploaded, **🎯 Balloon Part**, **⚡ Whole Sheet** and **⬚ Balloon Area** automatically **keep the customer's numbers**:

- Each numbered balloon is matched to the callout element it sits against: a size, a depth (`↧16.5 MAX`), a thread (`M3X0.5-6H`) or a frame. When the thread and the depth of one callout carry separate balloons, each gets its own characteristic.
- Your balloons are placed exactly over the customer's balloons, at the same size, so the report numbering matches the print.
- Gaps in the sequence are reported, e.g. "Numbers not on the drawing: 29, 45". These are usually deleted by a revision, so you can confirm nothing is missing.
- A double-outlined balloon is flagged so you can check the customer's requirements for what it means.
- Reference sizes in parentheses (`7X (Ø4.2)`) are recognised as reference. Callouts the customer did not balloon are listed unchecked at the end of the review list as a cross-check.
- Metric drawings are fully supported: whole-number dimensions (`10 +0.13/-0.01`, `13 ±1`), the `X ± 2` whole-number title-block tolerance (new **X (whole) ±** header field), units set to mm, all-numeric drawing numbers (`01234567-030`), "NOTES UNLESS OTHERWISE SPECIFIED" with `7A`/`7B` sub-notes, and `SPX-` specifications for Form 2.

### GD&T frames

- **Composite / multi-row frames:** each row is its own characteristic and inherits the symbol. `⌖ | Ø.010Ⓜ A B C` over `| Ø.000Ⓜ A` becomes `⌖ Ø.010Ⓜ A B C` and `⌖ Ø.000Ⓜ A`. This works when the symbol is on the first row and when it sits in its own cell spanning the rows. Datum cells written as separate text are joined to their row.
- **MMC / LMC (Ⓜ Ⓛ on the tolerance):** results within the stated tolerance are accepted automatically. A result over it is **never auto-rejected**, because bonus tolerance may still make it good; the inspector works out the bonus and dispositions it. Modifiers on datums only (`A GⓂ`) keep normal accept/reject.

### Balloon size and placement

Automatic balloons are **sized from the callout's text height**, about 1.5× the lettering (the global **Balloon ×** control still scales everything). They are placed **beside the whole callout**, including the stacked tolerance, modifiers and frame cells. The side is chosen to be clear of other text, other balloons and part lines, facing away from the part view. You can still drag any balloon.

### Drawing Reader Rules (teach the reader new formats without a software update)

**⚙ Settings → 📚 Drawing Reader Rules…** (also **📚 Reader Rules…** on Form 3) opens a rule library. It is stored in the database, shared by every workstation, and grows as new drawing formats come in.

| Rule type | Use it for |
|---|---|
| Text fix (find → replace) | Correct text before it is read, e.g. one customer's scans reading `0/` for `Ø`. Plain or regex; can apply to OCR text only. |
| Ignore text | Text that must never be ballooned, e.g. model coordinates `Z = 43.7875`. |
| Extra callout pattern | Balloon text the reader would skip, e.g. `DRILL PER MS\d+`, with type and designator. |
| Characteristic table | A table on the drawing where each row is a characteristic (PMI schedule, dimension table). Build the requirement from columns, e.g. `{CHARACTERISTIC} {TOL.} {DATUM REFS}`; skip rows by pattern. |
| GD&T word → symbol | Spelled-out characteristics (FLATNESS → ⏥, PROFILE OF A SURFACE → ⌓ …); 18 are built in. |
| Read line-drawn (vector) text | CAD PMI / 3D-annotation exports that draw dimension text as lines (Siemens NX PMI view sheets). *always* OCRs those sheets in addition to the text layer; *never* turns it off. |
| Spec / process pattern | A customer's spec numbers for Form 2 and the CoC, e.g. `BAC\d{4}`. |
| Title block field | A revision / drawing number / title written in a format the reader misses, e.g. `REV:\s*(\w+)`. |

- **Applies to:** each rule can be limited to drawings whose text or file name matches a pattern (a customer, a drawing prefix such as `99X1234`, `PMI VIEW SHEET`), and/or PDFs made by a CAD program (Creator/Producer contains `NX`, `SolidWorks` …). Blank means every drawing.
- **Example drawings:** when a drawing doesn't read right, fix it with a rule, then **📎 Add Drawing File** or **📎 Add From This Report**. The app reads it with the current rules and shows the list; correct it and save. That is what the drawing must keep producing.
- **▶ Test All Examples** re-reads every example drawing and reports PASS, or FAIL with the missing / extra characteristics. Run it after changing rules, so fixing one customer never quietly breaks another.
- **Change log:** every add, edit, delete, import, example and restore is recorded with who, when and which workstation, with the before and after. **↶ Restore Selected Version** puts a rule back.
- **Export / Import** the library as a .json file for backup or another site. Example drawings are not exported, because they may be export-controlled.
- Rules apply on every PC as soon as they are saved (the next drawing read uses them). Offline PCs use the last copy, read-only.
- Without a rule, a vector PDF sheet whose text layer holds almost no dimensions but lots of line work is OCR'd automatically.

### GD&T when adding characteristics by hand

- Adding a balloon by hand (**➕ Add Balloon** / Ctrl+B) asks for the requirement right away. The window has a GD&T symbol bar (⌖ ⏥ ⏤ ○ ⌭ ⌒ ⌓ ⊥ ∥ ∠ ◎ ⌯ ↗ ⌰ Ⓜ Ⓛ Ⓟ Ø ± ° ⌴ ⌵ ↧ □), a type and a designator. Untick *Ask for the requirement…* to go back to typing in the table.
- **⌖ Feature control frame…** builds the frame without typing symbols: characteristic, Ø zone, tolerance, material condition, primary / secondary / tertiary datums with modifiers, projected / free-state / unequal-profile options and a "4X"-style prefix. A **composite** frame becomes two characteristics (e.g. `⌖ Ø.010Ⓜ A B C` and `⌖ Ø.000Ⓜ A`), each inspected separately.
- In the Form 3 table, the Requirement cell has a small **⌖** button with every symbol and the frame builder. **⌖ GD&T…** on the toolbar rebuilds the frame of the selected row.
- Limits are calculated for built frames like for read ones (e.g. ⏥ .020 → 0 to .020; MMC results are never auto-rejected).

### Form 1 index of part numbers from the BOM

**Form 1 → 📥 From BOM / Parts List** fills the index for an assembly straight from the attached BOM or separate parts list. That includes Boeing PDM / Douglas parts lists, application lists, Lockheed EPL / CEBOM and scans. It lists every sub-assembly, detail and purchased part below the report's part number, in levels (`1`, `1.1`, `1.2` …), with the quantity per its parent and each detail's stock / material / make-from in Notes. Raw stock and consumables are left for Form 2 and the CoC. Balloon Part uses the BOM automatically when a separate parts list is attached; otherwise it uses the parts list on the drawing.

### Inspecting several parts on one report (Inspection Report / Standard Report)

The **Parts inspected** bar above the Form 3 table sets up a lot of parts, for example 5–7 parts inspected together:

- **Parts inspected** is the number of parts (1–50). Form 3 then has one result column per part (**P1 … P5**).
- **Readings**:
  - **One value per part** gives one column per part.
  - **Low & high per part** gives a **Low** and a **High** column per part, for the lowest and highest reading taken on that feature (for example the min / max of a bore). Both readings are checked against the limits.
- **🏷 Serial Numbers…** enters each part's serial number. **Auto-number from the first…** fills them in sequence (SN-0101, SN-0102 …). The serials show in the column headings and on the report.
- Each part is judged on its own:
  - An out-of-tolerance reading is shown in red.
  - The **All Parts** column rolls up every part: **Reject** if any part is rejected, **Accept** once every part is measured and accepted.
  - The results cell shows the overall range, e.g. `.2460 – .2560 (5 of 5 parts)`.
- The status line under the bar shows every part: **✔ Accept**, **✘ REJECT (char 1)**, or how many readings are still missing.
- Changing a limit re-judges every part.
- Lowering the part count or switching to one value per part hides readings but never deletes them. Switch back and they return.
- The **Completeness Check** lists any part missing a reading, a low without its high, or a serial number.
- **📥 Results (CMM/Excel)…** imports readings into the parts:
  - Map an optional **Part no. / serial** column to place each row by serial number or part number. Without it, rows go to part 1, 2, 3 … in file order.
  - In low & high mode, a row can carry both readings (`.248 / .251`), there can be a separate **High** column, or two rows for the same part become its low and its high.
- Two inspectors can enter different parts of the same characteristic at the same time. Their readings are merged, not refused as a conflict.
- The **PDF** keeps the normal report page (with the roll-up results). The per-part dimensions follow in **groups of 5 parts**: characteristics 1…N for P1–P5, then a new page with characteristics 1…N for P6–P10, and so on for as many parts as were inspected. Each group shows the characteristic, requirement, LSL / USL and each part's reading (or its Low / High pair), with out-of-tolerance readings in red. **Page x of y** runs across the whole document, including the drawings and photos.
- **Excel** export adds a **Per-Part Readings** sheet.

### OCR learning (the reader learns from your corrections)

Every time someone corrects a callout that OCR read wrong, FAI Studio remembers it. The correction can be made in the Auto-Balloon review list or later in the Form 3 table. The lessons are kept in the shared database, so every workstation gets smarter together.

- **What is learned:** the misread word and its correction, for example `0.25O` → `0.250`, `+.OO5` → `±.005` or `l.5OO` → `1.500`. When the words can't be matched up, the whole callout is learned instead, e.g. `2XØ.250` → `2X Ø.250`.
- **What is not learned:**
  - **Digit changes** (`.250` → `.260`, `.25` → `.250`). Those depend on one picture and would teach the reader to change correct values.
  - **Edits that add or remove words** (`1.500` → `1.500 REF`).
  - **A completely different callout.**
- **When it applies:**
  - A new lesson is **Learning** until the same correction has been made on **2 different characteristics** (adjustable). It then turns **Active** and is applied automatically to new OCR reads on every workstation.
  - Each fixed read is marked **"OCR – verify · learned OCR fix (read '…')"**, so it is still checked.
  - The review list says how many reads were corrected.
- **It corrects itself:**
  - The app counts whether each applied fix was **kept** or **undone**.
  - A fix undone at least twice, and more often than kept, **switches itself off**.
  - People correcting the same read two different ways makes it ambiguous, so it isn't applied.
- **⚙ Settings → 🧠 OCR Learning…** shows everything learned: what OCR read, the correction, times corrected, times auto-applied, kept and undone.
  - You can **Activate now**, **Turn off**, **Edit**, **Add** a fix by hand, **Delete**, or **Make Reader Rule** to turn a fix into a Drawing Reader Rule scoped to one customer.
  - A **Test** box shows what a read would become.
  - Learning can be switched off, and the threshold changed, on the same screen.
  - Every change is recorded in the Reader Rules change log.
- **Offline:** the active fixes are applied while working offline. New lessons are only learned while connected to the server.

### Scanned drawings (OCR)

Scans are read in three directions (so vertical dimensions are found), then each callout is re-read close up at 400 dpi. Common misreads are corrected using drawing context: Ø read as 0/O, ± read as +/t, ° read as o, and O/S/l inside numbers. Boxed BASIC dims and GD&T frames are found from the lines in the image. In the review list, every value is shown **next to a picture of the callout cut from the drawing**; double-click the picture to enlarge it.

**🧪 OCR Setup** (⚙ Settings menu, or **🧪 OCR** above a drawing) sets the **OpenCV preprocessing** used before OCR, with a live before/after preview and a **Test read** that compares what OCR reads with and without the clean-up. The profiles are:

| Profile | Use for |
|---|---|
| Off | Clean, high-quality scans |
| Standard (default) | Most scans: deskew, despeckle, sharpen |
| Heavy clean-up | Dirty, yellowed, shadowed or faxed copies: background flattening, denoise, adaptive black & white, strong despeckle |
| Faint or thin text | Light pencil or faded prints and small lettering: background flattening, local contrast (CLAHE), thicken strokes, 1.5× enlarge |
| Custom | Any mix of the individual filters, including removing long ruling lines. Use that only for grid paper, because it can clip text that touches a box or leader line. |

The setting is shared by all workstations. Each setting keeps its own cache, so switching back is instant. All processing runs on the PC; nothing is sent out.

OCR for scanned drawings is in `requirements-ocr.txt` (RapidOCR, installed automatically by `install_windows.bat` and optional — if it can't install, everything else still works). Python 3.12 is recommended; 3.10–3.14 are supported. The first scan takes about 20–60 seconds per sheet. After that it's cached, so it's instant. Vector CAD PDFs don't use OCR at all.

## 3. Daily use

1. **＋ New Report** → choose *AS9102 FAI* or *Inspection Report* → pick the drawing (PDF / TIFF / package ZIP) → **Create Report**. You can leave the part number blank.
2. The app reads the drawing and asks **which part number you're ballooning**. It lists every detail, assembly and tabulated dash number it found, with the sheets and views for each. Pick one and it auto-balloons that part, pulls in its applicable notes, material and (for assemblies) child parts, and fills in the header and tolerances from the title block. You review each list before anything is added.
3. To balloon another part from the same drawing later, click **🎯 Balloon Part…** above the drawing. Use **⬚ Balloon Area** to box a view by hand.
   - **Made a mistake picking the balloons?**
     - **↺ Start Over** in the review list throws that list away and takes you back to the part picker. Nothing has been added yet at that point.
     - After you've clicked Add Balloons, **↶ Undo Last Ballooning** (Form 3) removes just the characteristics that run added and offers to run it again.
     - **🔄 Start Over…** (Form 3) deletes every characteristic and balloon on the report and starts ballooning from the beginning. Header, parts list, materials, attachments and CoC are kept.
     - You're warned if results have already been entered, and every deletion is recorded in History.
4. **Form 3** tab: fix requirements, add missing balloons (**➕ Add Balloon**, or Ctrl+B), then enter results and tooling.
5. **Form 1 / Form 2**: finish the header, materials, special processes and C of C numbers.
6. **✔ Completeness Check** → **Submit for Review** → reviewer clicks **Approve & Lock**.
7. **Certificate of Conformance** tab → **Build from drawing / BOM / notes**, fill in cert / lot numbers.
8. **Preview & Export** → pick the **Document** (any report type or the CoC) → **Export PDF** / **Export Excel**, or **📦 Export Several…**.

Company name, CAGE code, address and CoC wording (shared by all workstations), the Gage Library, the Vendor Library, OCR Setup, Connection, **Display Size** and Release Check-out are all under the **⚙ Settings** menu.

### Screen sizes

Form 3 is laid out to keep the characteristics table as tall as possible:
- One toolbar holds **Add, Delete, ▲ ▼** and three menus:
  - **🎈 Balloons ▾**: renumber, undo or start over;
  - **📥 Import ▾**: notes list, CMM / Excel results;
  - **🛠 Tools ▾**: GD&T frame, set gage, accept selected, reader rules.
- Search, the Show filter and the Parts controls share one line.
- **◧ Drawing** hides the drawing pane so the table gets the whole width. The split between table and drawing is remembered on each PC.
- In the drawing pane, **Whole sheet**, **Balloon an area** and **OCR setup** are in the menu next to **🎯 Balloon Part**.
- **Submit**, **Return for Correction** and **Reopen** only show when they can be used.

The window opens to fit the monitor it is on. On laptops and small screens (under about 1500 × 860) it opens maximised and uses shorter tab names. Toolbars and button rows wrap onto a second line instead of pushing the window wider. Form 1 and the CoC details re-flow into 1–4 columns as the window is resized. Every dialog is capped to the screen. **⚙ Settings → Display Size** (80–175 %) scales the whole interface on that PC, for high-resolution monitors or anyone who wants larger text. It takes effect after a restart.

## Project layout

```
run.py                      launcher
fai_studio/app.py           startup / connection
fai_studio/db.py            PostgreSQL repository, field-level conflict detection, audit, live listener
fai_studio/schema.sql       tables + NOTIFY triggers (idempotent)
fai_studio/tolerance.py     requirement parser + accept/reject logic + spec finder
fai_studio/drawing_intel.py drawing understanding: title block, zones, parts list, tab tables, notes/flags,
                            view segmentation + titles -> part scoping, callout extraction
fai_studio/ocr.py           OCR engine wrapper (RapidOCR / Tesseract) with on-disk cache
fai_studio/extract.py       simple whole-page callout finder (legacy helper)
fai_studio/importers.py     parts list / notes / results importers
fai_studio/ocr_learn.py     OCR learning memory: learns fixes from corrections to OCR reads
fai_studio/multipart.py     several parts per report: per-part readings, roll-up, per-part status, import mapping
fai_studio/reports.py       PDF builder (Forms 1-3, inspection report, ballooned drawings, photos)
fai_studio/bom.py           parts lists / BOMs / notes lists (text + scanned) -> parts tree, Form 1 index
fai_studio/rules.py         Drawing Reader Rules library (types, scope, tables, GD&T words, testing)
fai_studio/coc.py           Certificate of Conformance rows (scoped to detail / assembly) + CoC PDF
fai_studio/excel_export.py  Excel export
fai_studio/ui/              PySide6 interface
samples/                    sample drawing, TIFF scan, photos, parts list, notes, CMM results
tests/                      pytest unit tests (parser, importers, extraction)
```
