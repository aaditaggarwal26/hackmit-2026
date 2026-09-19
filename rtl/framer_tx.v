// framer_tx: message -> type || payload || crc16 (LE) -> COBS -> 0x00, one
// byte per clock into the TX byte FIFO. The pre-COBS frame is first copied
// into a small byte array (computing the CRC on the way, one byte per
// clock), because COBS needs to know where the next zero is before it can
// emit a block's code byte. The encoder then alternates: scan forward to
// the next zero (or the end) to find the code, emit the code, copy the
// block. A trailing zero yields a final code 0x01, exactly like
// orbit/protocol/cobs.py. Code 0xFF never occurs because every frame is
// <= 253 bytes pre-COBS (protocol.md §2.3), so that case is not implemented.
//
// `ready` is only raised with room in the FIFO for the largest possible
// wire frame, so a started frame never stalls half-written. A single 0x00
// (link-open delimiter) is sent once after reset.

module framer_tx #(
    parameter MAX_PAYLOAD = 30,             // largest N->O payload (RUN_DONE)
    parameter FIFO_ADDR_W = 12
) (
    input  wire        clk,
    input  wire        rst_n,
    // message in
    input  wire        msg_start,
    input  wire [7:0]  msg_type,
    input  wire [5:0]  msg_len,             // payload bytes, <= MAX_PAYLOAD
    input  wire [8*MAX_PAYLOAD-1:0] msg_payload,   // byte 0 in [7:0]
    output wire        ready,
    // FIFO write side
    output reg         fifo_wr,
    output reg  [7:0]  fifo_wdata,
    input  wire [FIFO_ADDR_W:0] fifo_count
);
    localparam MAX_PRE  = MAX_PAYLOAD + 3;          // type + payload + crc
    localparam MAX_WIRE = MAX_PRE + 2;              // + COBS code + delimiter
    localparam IW = 6;                              // index width (MAX_PRE <= 63)

    localparam T_INIT = 3'd0, T_IDLE = 3'd1, T_LOAD = 3'd2, T_CRC1 = 3'd3, T_CRC2 = 3'd4,
               T_SCAN = 3'd5, T_COPY = 3'd6, T_DELIM = 3'd7;
    reg [2:0]  state;
    reg [7:0]  pre [0:MAX_PRE];                     // one spare so pre[total] is in range
    reg [IW-1:0] i, n, total, idx, j, p;

    // Room for a worst-case frame: count + MAX_WIRE <= capacity. fifo_count
    // lags a write by one cycle, and the previous frame's delimiter is still
    // being written in the first T_IDLE cycle, so `ready` is held off while
    // fifo_wr is high: otherwise a caller that saw ready=1 exactly at the
    // full boundary would pulse msg_start into a `room`=0 cycle and the
    // frame would vanish (seen once in ~1000 alerts at 1024 x 240).
    wire room = (fifo_count + MAX_WIRE) <= (1 << FIFO_ADDR_W);
    assign ready = (state == T_IDLE) && room && !fifo_wr;

    // The message is latched whole on msg_start so the caller may change its
    // selection the very next cycle; byte i of the pre-CRC frame is then
    // read from the latch during T_LOAD.
    reg  [8*(MAX_PAYLOAD+1)-1:0] msg_q;
    wire [7:0] load_byte = msg_q[8*i +: 8];

    wire        crc_clear = (state == T_IDLE);
    wire        crc_feed  = (state == T_LOAD);
    wire [15:0] crc;
    crc16 crc_i (.clk(clk), .rst_n(rst_n), .clear(crc_clear), .byte_valid(crc_feed), .byte_in(load_byte), .crc(crc));

    wire [7:0] pre_j = pre[j];
    wire [7:0] pre_p = pre[p];

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= T_INIT;
            fifo_wr <= 1'b0;
            fifo_wdata <= 8'd0;
            i <= {IW{1'b0}}; n <= {IW{1'b0}}; total <= {IW{1'b0}};
            idx <= {IW{1'b0}}; j <= {IW{1'b0}}; p <= {IW{1'b0}};
        end else begin
            fifo_wr <= 1'b0;
            case (state)
                T_INIT: begin                       // link-open delimiter
                    fifo_wr <= 1'b1;
                    fifo_wdata <= 8'd0;
                    state <= T_IDLE;
                end
                T_IDLE: begin
                    if (msg_start && room) begin
                        msg_q <= {msg_payload, msg_type};
                        n <= msg_len + 1'b1;           // type + payload (<= MAX_PRE - 2 < 64)
                        i <= {IW{1'b0}};
                        state <= T_LOAD;
                    end
                end
                T_LOAD: begin
                    pre[i] <= load_byte;
                    i <= i + 1'b1;
                    if (i == n - 1'b1) state <= T_CRC1;
                end
                T_CRC1: begin                       // crc register is final one cycle after the last feed
                    pre[n] <= crc[7:0];
                    state <= T_CRC2;
                end
                T_CRC2: begin
                    pre[n + 1'b1] <= crc[15:8];
                    total <= n + 6'd2;
                    idx <= {IW{1'b0}};
                    j <= {IW{1'b0}};
                    state <= T_SCAN;
                end
                T_SCAN: begin                       // find the end of the block starting at idx
                    if (j == total || pre_j == 8'd0) begin
                        fifo_wr <= 1'b1;
                        fifo_wdata <= {2'b0, j - idx} + 1'b1;   // code = block length + 1
                        p <= idx;
                        state <= T_COPY;
                    end else begin
                        j <= j + 1'b1;
                    end
                end
                T_COPY: begin
                    if (p != j) begin
                        fifo_wr <= 1'b1;
                        fifo_wdata <= pre_p;
                        p <= p + 1'b1;
                    end else if (j == total) begin
                        state <= T_DELIM;
                    end else begin                  // skip the zero, next block
                        idx <= j + 1'b1;
                        j <= j + 1'b1;
                        state <= T_SCAN;
                    end
                end
                T_DELIM: begin
                    fifo_wr <= 1'b1;
                    fifo_wdata <= 8'd0;
                    state <= T_IDLE;
                end
                default: state <= T_IDLE;
            endcase
        end
    end
endmodule
