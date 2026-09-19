# bin/

存放各平台的 llama-server 可执行文件，不入库：

- `linux-x86_64/llama-server`：由 `build-llama-server` workflow 在 manylinux_2_28 容器中编译，可直接部署到统信 UOS v20。
- `windows-x86_64/llama-server.exe`：开发机使用 llama.cpp 官方预编译包。

产物旁应附 `BUILD_INFO.txt` 与 `SHA256SUMS`。
