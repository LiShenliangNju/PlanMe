import ollama

# 检查服务
try:
    client = ollama.Client(host='http://localhost:11434')
    print(f"✅ 服务正常,{client.list()}")
except Exception as e:
    print(f"❌ 连接失败: {e}")
    exit()

# 生成回复
response = client.generate(model='qwen2.5:7b', prompt='Hello')
print(response['response'])