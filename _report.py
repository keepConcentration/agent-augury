import subprocess, os, tempfile

thread_id = '1542362411728904273'
content = '''<@1526399531271323708>
【다해핑 보고】 task=t_10631fe1 status=assigned
- 제목: agent-augury v0.1b 마무리: Fake에서 진짜 협업으로
- 결과/사유: 칸반 할당받아 작업 시작
- 변경: 없음 (분석 단계)
- 검증: 없음
- 리스크/장애: 없음
'''
fd, path = tempfile.mkstemp(suffix='.txt')
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write(content)
result = subprocess.run(['hermes', 'send', '--to', f'discord:{thread_id}', '--file', path], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
os.remove(path)
