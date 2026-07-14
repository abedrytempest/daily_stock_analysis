import smtplib, os
from email.mime.text import MIMEText

report = open('reports/relay_analysis.md', encoding='utf-8').read()
sender = os.environ['EMAIL_SENDER']
pwd = os.environ['EMAIL_PASSWORD']
to = os.environ.get('EMAIL_RECEIVERS', sender)

msg = MIMEText(report, 'plain', 'utf-8')
msg['Subject'] = '🎯 短线接力分析'
msg['From'] = sender; msg['To'] = to

# Auto-detect SMTP
domain = sender.split('@')[-1].lower()
smtp_map = {
    'qq.com': ('smtp.qq.com', 465),
    '163.com': ('smtp.163.com', 465),
    '126.com': ('smtp.126.com', 465),
    'gmail.com': ('smtp.gmail.com', 587),
}
host, port = smtp_map.get(domain, ('smtp.qq.com', 465))

s = smtplib.SMTP_SSL(host, port, timeout=30)
s.login(sender, pwd)
s.sendmail(sender, to.split(','), msg.as_string())
s.quit()
print(f'✅ 邮件已发送 via {host} → {to}')
