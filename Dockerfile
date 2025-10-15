# 第一阶段：构建Python依赖
FROM python:3.10-slim AS builder

# 安装系统依赖，包括编译工具
#RUN apt-get update && apt-get install -y \
#    gcc \
#    g++ \
#    make \
#    && rm -rf /var/lib/apt/lists/*
RUN echo "deb http://mirrors.tuna.tsinghua.edu.cn/debian trixie main contrib non-free" > /etc/apt/sources.list \
    && echo "deb http://mirrors.tuna.tsinghua.edu.cn/debian trixie-updates main contrib non-free" >> /etc/apt/sources.list \
    && echo "deb http://mirrors.tuna.tsinghua.edu.cn/debian-security trixie-security main contrib non-free" >> /etc/apt/sources.list \
    && apt-get update && apt-get install -y gcc g++ make && rm -rf /var/lib/apt/lists/*



# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -r requirements.txt

# 第二阶段：运行阶段
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 从构建阶段复制Python依赖
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# 创建必要的目录
RUN mkdir -p tmp data

# 复制应用代码
COPY . .

# 启动命令
CMD ["python", "start_server.py"]
