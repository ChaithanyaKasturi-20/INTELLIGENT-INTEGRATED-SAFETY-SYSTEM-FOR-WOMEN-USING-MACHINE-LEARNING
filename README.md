# Intelligent Integrated Safety System for Women Using Machine Learning

A Django web application for women safety support, including login/signup, route search, crime prediction, emergency panic reporting, and ML visualizations.

## Features

- User registration and login
- Emergency panic reporting
- Route lookup and map embedding
- Crime prediction page
- Heatmap and model performance charts
- SQLite-based local database setup

## Tech Stack

- Python 3.9
- Django 2.1.7
- SQLite3
- Pandas, NumPy, Seaborn, Matplotlib

## Local Setup

1. Open a terminal in the project folder.
2. Create or activate the virtual environment:

   ```powershell
   .\.venv39\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Create a local `.env` file in the project root with your Gmail app password:

   ```env
   PANIC_EMAIL_USER=yourgmail@gmail.com
   PANIC_EMAIL_PASS=your_16_char_app_password
   ```

   Important: use a Gmail App Password, not your normal Gmail password.

5. Run database setup and start the app:

   ```powershell
   python manage.py migrate
   python manage.py runserver 0.0.0.0:8001
   ```

6. Open:

   ```text
   http://127.0.0.1:8001/index.html
   ```

## Notes

- This project uses legacy Django URL patterns such as `/index.html`, `/UserLogin.html`, and `/Register.html`.
- The panic feature requires the user to be logged in before sending an emergency alert.
- Do not commit your `.env` file to GitHub.

## GitHub

```bash
git init
git branch -M main
git remote add origin https://github.com/ChaithanyaKasturi-20/INTELLIGENT-INTEGRATED-SAFETY-SYSTEM-FOR-WOMEN-USING-MACHINE-LEARNING.git
git add .
git commit -m "Initial commit"
git push -u origin main
```
