# HS300 Kasa Dashboard

## How to use
1. Clone the repository:
   ```bash
   git clone <repository-url>
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up environment variables:
    ```bash
    sudo service mysql start
    mv database.example database.config
    mv device.example device.config
    ```
    Edit `database.config` and `device.config` with your database and device information.
4. Run the script:
    ```bash
    python app.py
    ```
5. Access the dashboard in your browser at `http://localhost:5000` or `http://<your-ip>:5000`.

## Features
1. Display HS300 every sockets informations & device informations in a table format.
2. On / Off socket power.
3. Not at home mode. (I don't know if it works.)
4. Timer. (I don't know if it works.)
5. Rename socket / groups.
6. Schedule socket on / off. (I don't know if it works.)
7. Turn On / Off all sockets power. (At the same time.)
8. Show offline HS300 devices.
9. Group control for HS300 devices.

## Credits
- [python-kasa](https://github.com/python-kasa/python-kasa)
- [TP-Link](https://www.tp-link.com)
- [TP-Link Smart Home](https://www.home-assistant.io/integrations/tplink/)
- [EdwardWu](https://github.com/bluehomewu)

## License
This project is licensed under GNU General Public License v3.0. See the [LICENSE](LICENSE) file for details.

