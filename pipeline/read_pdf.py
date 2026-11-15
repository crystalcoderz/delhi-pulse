from pypdf import PdfReader

r = PdfReader(r"C:\Users\praya\AppData\Local\Temp\instructions.pdf")
print("pages:", len(r.pages))
for i, p in enumerate(r.pages[:5]):
    print(f"---- page {i+1} ----")
    print((p.extract_text() or "")[:1600])
