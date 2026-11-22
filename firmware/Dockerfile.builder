FROM espressif/idf:v5.5.6

WORKDIR /project
COPY . /project

# Build universal ESP32-C5 firmware (no secrets.h needed — Wi-Fi & bridge IP configured in NVS)
RUN idf.py set-target esp32c5 && \
    idf.py build && \
    mkdir -p /out && \
    cp build/sureflap_hub.bin /out/sureflap_hub.bin && \
    grep -o '"[^"]*"' CMakeLists.txt | head -n1 | tr -d '"' > /out/ver.txt && \
    esptool.py --chip esp32c5 merge_bin -o /out/sureflap_hub_merged.bin \
      --flash_mode dio --flash_freq 80m --flash_size 4MB \
      0x2000 build/bootloader/bootloader.bin \
      0x8000 build/partition_table/partition-table.bin \
      0xf000 build/ota_data_initial.bin \
      0x20000 build/sureflap_hub.bin

CMD ["sh", "-c", "cp -v /out/sureflap_hub.bin /out/sureflap_hub_merged.bin /out/ver.txt /output/ && echo 'Copied firmware binaries + ver.txt to /output/'"]
