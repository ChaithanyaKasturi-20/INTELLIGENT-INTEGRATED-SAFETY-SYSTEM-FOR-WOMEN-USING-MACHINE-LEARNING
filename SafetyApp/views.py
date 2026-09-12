from django.shortcuts import render
from django.template import RequestContext
from django.contrib import messages
from django.http import HttpResponse
from django.conf import settings
import os
import io
import base64
import sqlite3
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import pickle
import smtplib
from datetime import date
import seaborn as sns

global username, email
dataset = pd.read_csv("Dataset/CrimesOnWomenData.csv")
states = np.unique(dataset['State'])


def load_local_env():
    env_path = os.path.join(settings.BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return

    with open(env_path, 'r', encoding='utf-8') as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_panic_email_config():
    load_local_env()
    sender_email = (os.environ.get('PANIC_EMAIL_USER', '') or '').strip()
    sender_password = ''.join(ch for ch in (os.environ.get('PANIC_EMAIL_PASS', '') or '') if not ch.isspace())
    return sender_email, sender_password


def get_register_db_connection():
    conn = sqlite3.connect(os.path.join(settings.BASE_DIR, 'db.sqlite3'))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_register_table():
    with get_register_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS register (
                username TEXT PRIMARY KEY,
                password TEXT,
                contact_no TEXT,
                email TEXT,
                address TEXT
            )
        """)
        conn.commit()

def Route(request):
    if request.method == 'GET':
        return render(request, 'Route.html', {})

def RouteAction(request):
    if request.method == 'POST':
        areaname = request.POST.get('t1', False)
        areaname = areaname+" Police Station"
        areaname = areaname.replace(" ","+")
        output = '<iframe width="800" height="650" frameborder="0" scrolling="no" marginheight="0" marginwidth="0" src="https://maps.google.com/maps?q='+areaname+'&amp;ie=UTF8&amp;&amp;output=embed"></iframe><br/>'
        context= {'data':output}
        return render(request, 'UserScreen.html', context)

def Heatmap(request):
    if request.method == 'GET':
        global dataset, states
        heatmap = dataset.groupby('State')['Rape'].sum().sort_values(ascending=False).reset_index(name="Total Crimes")[0:10]
        labels = heatmap['State'].ravel()
        heatmap.drop(['State'], axis = 1,inplace=True)
        heatmap = heatmap.values
        data = []
        for i in range(len(heatmap)):
            values = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
            values[i] = heatmap[i,0]
            data.append(values)
        heatmap = np.asarray(data)    
        ax = sns.heatmap(heatmap, xticklabels = labels, yticklabels = labels, annot = True, cmap="viridis" ,fmt ="g");
        ax.set_ylim([0,len(heatmap)])
        plt.title("Women Incident Crime Heatmap")
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        #plt.close()
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        plt.clf()
        plt.cla()
        context= {'data':'Women Incident Crime Heatmap', 'img': img_b64}
        return render(request, 'UserScreen.html', context)

def TrainML(request):
    if request.method == 'GET':
        f = open('model/model_history.pckl', 'rb')
        hist = pickle.load(f)
        f.close()
        plt.figure(figsize=(5,3))
        plt.plot(hist['accuracy'], color = 'blue', label = 'Training Accuracy')
        plt.plot(hist['val_accuracy'], color = 'green', label = 'Validation Accuracy')
        plt.title('Neural Network Accuracy Graph')
        plt.xlabel('Epochs')
        plt.ylabel('Accuracy')
        plt.legend()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        #plt.close()
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        plt.clf()
        plt.cla()
        context= {'data':'Neural Network Accuracy Graph', 'img': img_b64}
        return render(request, 'UserScreen.html', context)

def CrimePredict(request):    
    if request.method == 'GET':
        global dataset, states
        output = '<tr><td><font size="3" color="black">Choose&nbsp;State</td><td><select name="t1">'
        for i in range(len(states)):
            output += '<option value="'+states[i]+'">'+states[i]+'</option>'
        output +="</select></td></tr>"
        context= {'data1':output}
        return render(request, 'CrimePredict.html', context)

def CrimePredictAction(request):
    if request.method == 'POST':
        global dataset
        areaname = request.POST.get('t1', False)
        crime = dataset.loc[dataset['State'] == areaname]
        crime = crime['Rape'].ravel()[0]
        output = "<font size=3 color=blue>Predicted Number of Crimes on Women in "+areaname+" = "+str(crime)+"</font>"
        context= {'data':output}
        return render(request, 'UserScreen.html', context)

def Panic(request):
    if request.method == 'GET':
        return render(request, 'Panic.html', {})

def PanicAction(request):
    if request.method == 'POST':
        global username, email
        areaname = request.POST.get('t1', False)

        if not username or not email:
            context = {'data': 'Please login first before sending an emergency alert.'}
            return render(request, 'Panic.html', context)

        sender_email, sender_password = get_panic_email_config()
        message_text = username + " sent emergency message from location " + areaname + " to you."

        if not sender_email or not sender_password:
            context = {'data': 'Emergency alert recorded for ' + username + '. Email sending is not configured on this machine.'}
            return render(request, 'Panic.html', context)

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as connection:
                connection.login(sender_email, sender_password)
                connection.sendmail(from_addr=sender_email, to_addrs=[email], msg=message_text)
            context = {'data': 'Emergency message sent to registered E-Mail'}
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPException, OSError) as exc:
            context = {'data': 'Emergency alert recorded for ' + username + '. Email could not be sent because SMTP credentials are invalid or unavailable.'}

        return render(request, 'Panic.html', context)

def UserLoginAction(request):
    global username
    if request.method == 'POST':
        global username, email
        status = "none"
        users = request.POST.get('t1', False)
        password = request.POST.get('t2', False)
        ensure_register_table()
        with get_register_db_connection() as con:
            cur = con.cursor()
            cur.execute("SELECT username, password, email FROM register")
            rows = cur.fetchall()
            for row in rows:
                if row['username'] == users and row['password'] == password:
                    email = row['email']
                    username = users
                    status = "success"
                    break
        if status == 'success':
            context= {'data':'Welcome '+username}
            return render(request, "UserScreen.html", context)
        else:
            context= {'data':'Invalid username'}
            return render(request, 'UserLogin.html', context)

def RegisterAction(request):
    if request.method == 'POST':
        global username
        username = request.POST.get('t1', False)
        password = request.POST.get('t2', False)
        contact = request.POST.get('t3', False)
        email = request.POST.get('t4', False)
        address = request.POST.get('t5', False)

        ensure_register_table()
        output = "none"
        with get_register_db_connection() as con:
            cur = con.cursor()
            cur.execute("SELECT username FROM register")
            rows = cur.fetchall()
            for row in rows:
                if row['username'] == username:
                    output = username+" Username already exists"
                    break
        if output == "none":
            with get_register_db_connection() as db_connection:
                db_cursor = db_connection.cursor()
                db_cursor.execute(
                    "INSERT INTO register (username, password, contact_no, email, address) VALUES (?, ?, ?, ?, ?)",
                    (username, password, contact, email, address)
                )
                db_connection.commit()
                if db_cursor.rowcount == 1:
                    output = "Signup process completed. Login to perform Emergency activities"
        context= {'data':output}
        return render(request, 'Register.html', context)       

def UserLogin(request):
    if request.method == 'GET':
       return render(request, 'UserLogin.html', {})

def index(request):
    if request.method == 'GET':
       return render(request, 'index.html', {})

def Register(request):
    if request.method == 'GET':
       return render(request, 'Register.html', {})



    

