# 🌐 VPN Gate SSTP Collector & Auto-Updater

This project is an advanced automation tool designed to scrape, validate, and accumulate **SSTP VPN** server addresses from [VPN Gate](https://www.vpngate.net/).

It maintains an **accumulative database**, meaning it remembers servers from previous scans, allowing the list to grow beyond the standard limits.

## 📥 Direct Download

You can download the latest updated list of SSTP servers directly from the link below:

🔗 **[Download sstp_hosts.txt](https://raw.githubusercontent.com/Delta-Kronecker/Vpn-Gate/refs/heads/main/sstp_hosts.txt)**

---

# 📢 Telegram Channel

All final results are automatically compressed into a ZIP file and uploaded to our Telegram channel:

🔹 **Main Channel:** [DeltaKroneckerFreedom](https://t.me/GitKroneckerDelta)  

---

## 💻 How to Use (Windows Native VPN)

You don't need any extra software. You can connect using the built-in Windows VPN settings:

1. **Get a Server:** Open the `sstp_hosts.txt` file linked above and copy one server address (e.g., `vpn123.opengw.net:443` or `public-vpn-229.opengw.net`).
2. **Open Settings:** Go to **Settings** > **Network & Internet** > **VPN**.
3. **Add Connection:** Click on **"Add a VPN connection"**.
4. **Fill Details:**
   - **VPN Provider:** Windows (built-in)
   - **Connection Name:** Any name (e.g., VPN Gate)
   - **Server Name or Address:** Paste the address you copied in Step 1.
   - **VPN Type:** Secure Socket Tunneling Protocol (SSTP)
   - **Type of sign-in info:** User name and password
   - **User name:** `vpn`
   - **Password:** `vpn`
5. **Connect:** Click **Save**, then select the connection and click **Connect**.

> **Note:** If a server fails to connect, simply try another address from the list.

## ⚠️ Disclaimer
This project is for educational and research purposes only. The developers are not responsible for any misuse of this tool.

## 🔥 Keep This Project Going!

If you're finding this useful, please show your support:

⭐ **Star the repository on GitHub**

⭐ **Star our [Telegram posts](https://t.me/DeltaKroneckerGithub)** 

Your stars fuel our motivation to keep improving!
