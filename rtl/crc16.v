// crc16: CRC-16/CCITT-FALSE, poly 0x1021, init 0xFFFF, no reflection, no
// xorout. One byte per clock: the eight shift steps of orbit/protocol/crc16.py
// are unrolled into one combinational function, so the register updates the
// cycle after `byte_valid`. Check value: "123456789" -> 0x29B1.

module crc16 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       clear,          // back to 0xFFFF (takes priority)
    input  wire       byte_valid,
    input  wire [7:0] byte_in,
    output reg [15:0] crc
);
    function [15:0] crc_step;
        input [15:0] c;
        input [7:0]  b;
        integer i;
        reg [15:0] x;
        begin
            x = c ^ {b, 8'h00};
            for (i = 0; i < 8; i = i + 1)
                x = x[15] ? ((x << 1) ^ 16'h1021) : (x << 1);
            crc_step = x;
        end
    endfunction

    always @(posedge clk) begin
        if (!rst_n || clear)  crc <= 16'hFFFF;
        else if (byte_valid)  crc <= crc_step(crc, byte_in);
    end
endmodule
