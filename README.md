# CEI PDF Signer

**[🌐 Website](https://bancuthekind.github.io/cei-pdf-signer)** | **[📥 Download](https://github.com/BancuTheKind/cei-pdf-signer/releases)**

Free, open-source macOS application for digitally signing PDF documents using the Romanian Electronic Identity Card (CEI).

---

🇬🇧 **[English](#english)** | 🇷🇴 **[Română](#română)**

---

## English

### About

CEI PDF Signer allows you to digitally sign PDF documents using the qualified certificate from your Romanian Electronic Identity Card. The app runs natively on macOS and uses the PKCS#11 library from IDEMIA.

### Features

- Modern, intuitive web interface
- Sign multiple PDF documents at once
- Visual signature placement on each document
- Page navigation: jump to any page number, first page, last page
- Apply the signature mark to every page of a document in one click
- Support for ECDSA certificates from CEI
- Automatic smart card reader detection
- Direct export to Downloads folder
- Configurable PKCS#11 library path

### Requirements

#### Hardware
- USB smart card reader
- Romanian Electronic Identity Card (CEI)

#### Software
- macOS 10.13 or newer
- [IDPlugManager](https://www.inteligent.ro/idplugmanager/) - official CEI software (provides the PKCS#11 library)

### Installation

#### Option 1: Pre-built Application (Recommended)

1. Download the latest release from [Releases](../../releases)
2. Extract and move `CEI PDF Signer.app` to `/Applications`
3. On first run, right-click → Open (to allow execution)

#### Option 2: From Source

```bash
git clone https://github.com/BancuTheKind/cei-pdf-signer.git
cd cei-web-signer
./run.sh
```

### Usage

1. **Connect your card reader** and insert your CEI
2. **Launch the app** - it will automatically detect your card
3. **Load PDFs** - drag & drop or click to select
4. **Draw signature area** - click and drag on each document
5. **Click "Sign Files"** - enter your PIN (6 digits) and wait
6. **Download** - signed files are saved to Downloads

### PIN Information

- **Signature PIN (6 digits)**: for signing documents (Slot 2)
- **Authentication PIN (4 digits)**: for online authentication (Slot 0)

### Troubleshooting

#### "No smart card detected"
- Verify the reader is connected
- Verify the CEI is properly inserted
- Reinstall IDPlugManager

#### "PKCS11 library not found"
- Verify IDPlugManager is installed
- Open Settings and check/update the PKCS#11 library path

#### macOS blocks the reader / App hangs

If macOS takes control of the reader (shows "Smart card detected" notification) or the app hangs on startup:

**First, try this — it fixes it almost every time:** unplug the reader and plug it back
in, preferably into a USB port on the Mac itself rather than through a hub. If the reader
is visible to macOS but no card is found, re-seat the card in the reader.

**Do not kill CryptoTokenKit.** `pkill ctkd` (or `ctkpcscd`, or `ctkahp`) takes down the
PC/SC service that CryptoTokenKit provides, which makes the reader **completely invisible
to every application** until it is physically re-plugged. It does not help: PKCS#11 access
works fine while CryptoTokenKit is running. Verified on macOS 26.5 with IdPlug 2.7.0 —
loading the library takes 0.2s and enumerating slots 2–20s, with all CryptoTokenKit
daemons alive and untouched.

Slot enumeration being slow is normal. The IdPlug driver discovers the card's applications
progressively: the first read often shows only slot 1, with the signature slot appearing
several seconds later. The app polls until it settles, so the first detection after
inserting a card can take 10–20 seconds.

**Only if re-plugging does not help**, you can disable macOS's smart-card services. This is
a persistent, system-wide change that affects every application, so treat it as a last
resort:
```bash
sudo defaults write /Library/Preferences/com.apple.security.smartcard allowSmartCard -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard UserPairing -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard useIFDCCID -bool false
# Restart your Mac for changes to take effect
```
Reverse it with the same commands using `-bool true`.

#### Debugging CryptoTokenKit

To view macOS CryptoTokenKit debug logs (useful for diagnosing smart card issues):

```bash
./scripts/ctk-logs.sh
```

### Security

- PIN is never stored
- Communication is local only (localhost)
- Private key never leaves the smart card
- Code is open-source for audit

---

## Română

### Despre

CEI PDF Signer permite semnarea digitala a documentelor PDF folosind certificatul calificat de pe Cartea de Identitate Electronica romaneasca. Aplicatia functioneaza pe macOS si foloseste biblioteca PKCS#11 de la IDEMIA.

### Caracteristici

- Interfata web moderna si intuitiva
- Semnare multipla documente PDF
- Selectare vizuala a pozitiei semnaturii pe document
- Navigare pagini: salt la orice numar de pagina, prima pagina, ultima pagina
- Aplicarea semnaturii pe toate paginile documentului dintr-un singur click
- Suport pentru certificatele ECDSA de pe CEI
- Detectare automata a cititorului de carduri
- Export direct in folderul Downloads
- Configurare cale biblioteca PKCS#11

### Cerinte

#### Hardware
- Cititor de carduri smart card (USB)
- Cartea de Identitate Electronica (CEI) din Romania

#### Software
- macOS 10.13 sau mai nou
- [IDPlugManager](https://www.inteligent.ro/idplugmanager/) - software-ul oficial pentru CEI (instaleaza biblioteca PKCS#11)

### Instalare

#### Varianta 1: Aplicatie compilata (recomandat)

1. Descarcati ultima versiune din [Releases](../../releases)
2. Dezarhivati si mutati `CEI PDF Signer.app` in `/Applications`
3. La prima rulare, click dreapta -> Open (pentru a permite rularea)

#### Varianta 2: Din sursa

```bash
git clone https://github.com/BancuTheKind/cei-pdf-signer.git
cd cei-web-signer
./run.sh
```

### Utilizare

1. **Conectati cititorul de carduri** si introduceti CEI-ul
2. **Lansati aplicatia** - va detecta automat cardul
3. **Incarcati PDF-urile** - drag & drop sau click pentru selectare
4. **Desenati zona semnaturii** - click si drag pe fiecare document
5. **Click "Sign Files"** - introduceti PIN-ul (6 cifre) si asteptati
6. **Descarcati** - fisierele semnate vor fi salvate in Downloads

### PIN-uri CEI

- **PIN Semnatura (6 cifre)**: pentru semnarea documentelor (Slot 2)
- **PIN Autentificare (4 cifre)**: pentru autentificare online (Slot 0)

### Rezolvarea problemelor

#### "No smart card detected"
- Verificati ca cititorul este conectat
- Verificati ca CEI-ul este introdus corect in cititor
- Reinstalati IDPlugManager

#### "PKCS11 library not found"
- Verificati ca IDPlugManager este instalat
- Deschideti Settings si verificati/actualizati calea catre biblioteca PKCS#11

#### macOS blocheaza cititorul / Aplicatia se blocheaza

Daca macOS preia controlul asupra cititorului (apare notificare "Smart card detected") sau aplicatia se blocheaza la pornire:

**Incercati intai asta — rezolva problema aproape de fiecare data:** scoateti cititorul si
introduceti-l din nou, de preferat intr-un port USB de pe Mac, nu printr-un hub. Daca
cititorul este vizibil dar cardul nu este gasit, reintroduceti cardul in cititor.

**Nu opriti CryptoTokenKit.** `pkill ctkd` (sau `ctkpcscd`, sau `ctkahp`) opreste serviciul
PC/SC furnizat de CryptoTokenKit, iar cititorul devine **complet invizibil pentru toate
aplicatiile** pana cand este scos si introdus din nou fizic. Nu ajuta: accesul PKCS#11
functioneaza normal cat timp CryptoTokenKit ruleaza. Verificat pe macOS 26.5 cu IdPlug
2.7.0 — incarcarea bibliotecii dureaza 0.2s, iar enumerarea sloturilor 2–20s, cu toate
procesele CryptoTokenKit pornite si neatinse.

Enumerarea lenta a sloturilor este normala. Driverul IdPlug descopera aplicatiile de pe card
progresiv: prima citire arata deseori doar slotul 1, iar slotul de semnare apare dupa cateva
secunde. Aplicatia reincearca pana se stabilizeaza, deci prima detectare dupa introducerea
cardului poate dura 10–20 de secunde.

**Doar daca reintroducerea cititorului nu ajuta**, puteti dezactiva serviciile de smart card
din macOS. Este o modificare permanenta, la nivel de sistem, care afecteaza toate
aplicatiile, deci folositi-o ca ultima solutie:
```bash
sudo defaults write /Library/Preferences/com.apple.security.smartcard allowSmartCard -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard UserPairing -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard useIFDCCID -bool false
# Restartati Mac-ul pentru ca setarile sa aiba efect
```
Se revine cu aceleasi comenzi folosind `-bool true`.

#### Debugging CryptoTokenKit

Pentru a vedea log-urile de debug macOS CryptoTokenKit (util pentru diagnosticarea problemelor cu smart card):

```bash
./scripts/ctk-logs.sh
```

### Securitate

- PIN-ul nu este stocat niciodata
- Comunicatia este doar locala (localhost)
- Cheia privata nu paraseste niciodata cardul smart
- Codul este open-source pentru audit

---

## License

MIT License - see [LICENSE](LICENSE)

---

Made with ❤️ for the Romanian community
