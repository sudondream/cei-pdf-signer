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
- [IDPlugManager](https://hub.mai.gov.ro/aplicatie-cei) - official CEI software (provides the PKCS#11 library)

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

**Cause:** macOS CryptoTokenKit tries to use the reader simultaneously.

**Solution:**
```bash
sudo defaults write /Library/Preferences/com.apple.security.smartcard allowSmartCard -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard UserPairing -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard useIFDCCID -bool false
# Restart your Mac for changes to take effect
```

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
- [IDPlugManager](https://hub.mai.gov.ro/aplicatie-cei) - software-ul oficial pentru CEI (instaleaza biblioteca PKCS#11)

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

**Cauza:** macOS CryptoTokenKit incearca sa foloseasca cititorul simultan cu aplicatia noastra.

**Solutie:**
```bash
sudo defaults write /Library/Preferences/com.apple.security.smartcard allowSmartCard -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard UserPairing -bool false
sudo defaults write /Library/Preferences/com.apple.security.smartcard useIFDCCID -bool false
# Restartati Mac-ul pentru ca setarile sa aiba efect
```

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
