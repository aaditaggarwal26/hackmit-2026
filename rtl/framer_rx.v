// framer_rx: UART bytes -> COBS decode -> CRC/type/length check -> message,
// implementing the error table of protocol.md §3 exactly as
// orbit.protocol.messages.FrameDecoder does (the counters must agree byte
// for byte with it, since STATUS reports them).
//
// COBS as a byte state machine: a code byte c says "copy c-1 bytes"; when a
// block ends and the NEXT byte is another code byte, a zero is emitted
// first (never before the delimiter). Code 0xFF cannot occur: frames are
// <= 253 bytes pre-COBS (§2.3), so the "no implicit zero after 0xFF" case
// is omitted on purpose. A delimiter that arrives inside a block is the
// Python decoder's "code runs past end" -> len_errors.
//
// The CRC covers every decoded byte except the last two, which are not known
// to be the last until the delimiter: decoded bytes pass through a two-deep
// delay line and the CRC is fed from its tail, so at the delimiter the
// register holds crc(body) and the delay line holds the received CRC (LE).
//
// Overflow counts ENCODED bytes like FrameDecoder: the 255th non-zero byte
// of a frame sets rx_overflow, and everything up to the next 0x00 is
// dropped. Counters are u16 and wrap.

module framer_rx #(
    parameter MAX_PAYLOAD = 131             // largest O->N payload (FRAME_INGEST / REF_FRAME_SET)
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        rx_valid,
    input  wire [7:0]  rx_data,
    output reg         msg_valid,           // one-cycle pulse
    output reg  [7:0]  msg_type,
    output reg  [8*MAX_PAYLOAD-1:0] msg_payload,
    output reg  [15:0] crc_errors,
    output reg  [15:0] len_errors,
    output reg  [15:0] unknown_type,
    output reg  [15:0] rx_overflow
);
    // expected payload length per type (protocol.md §4); bit 8 = unknown
    function [8:0] type_len;                // [8] = unknown flag; lengths up to 255
        input [7:0] t;
        begin
            case (t)
                8'h01: type_len = 9'd131;       // FRAME_INGEST
                8'h02: type_len = 9'd10;        // CONFIG_SET
                8'h03: type_len = 9'd131;       // REF_FRAME_SET
                8'h04: type_len = 9'd4;         // BENCH_RUN
                8'h10: type_len = 9'd0;         // STATUS_QUERY
                8'h11: type_len = 9'd30;        // STATUS_REPLY
                8'h12: type_len = 9'd14;        // FRAME_SCORED
                8'h13: type_len = 9'd9;         // BENCH_DONE
                8'h20: type_len = 9'd6;         // GRANT
                8'h21: type_len = 9'd11;        // TX_FRAME
                8'h22: type_len = 9'd10;        // TX_DONE
                8'h40: type_len = 9'd7;         // HEARTBEAT
                8'h41: type_len = 9'd10;        // POWER
                default: type_len = 9'h100;
            endcase
        end
    endfunction

    reg [7:0]  enc_count;                   // encoded bytes stored this frame (saturates at 255)
    reg        overflow;
    reg [7:0]  code_rem;                    // data bytes still to copy in this block
    reg        pending_zero;                // a block ended; emit 0 if another code byte follows
    reg [8:0]  dec_count;                   // decoded bytes so far
    reg [7:0]  d0, d1;                      // delay line: d1 newest
    reg        crc_feed;
    reg [7:0]  crc_byte;
    reg        crc_clear;

    wire [15:0] crc;
    crc16 crc_i (.clk(clk), .rst_n(rst_n), .clear(crc_clear), .byte_valid(crc_feed), .byte_in(crc_byte), .crc(crc));

    // Decoded-byte sink: byte 0 is the type, bytes 1.. go to the payload
    // buffer (the CRC bytes land there too and are simply never used).
    // Bytes arrive >= one UART character apart, so the registered crc_feed
    // and the crc16 register have settled long before the delimiter.

    wire [8:0]  tl = type_len(msg_type);
    wire        frame_end = rx_valid && (rx_data == 8'd0);

    integer b;
    always @(posedge clk) begin
        if (!rst_n) begin
            enc_count <= 8'd0; overflow <= 1'b0; code_rem <= 8'd0; pending_zero <= 1'b0;
            dec_count <= 9'd0; d0 <= 8'd0; d1 <= 8'd0;
            crc_feed <= 1'b0; crc_byte <= 8'd0; crc_clear <= 1'b1;
            msg_valid <= 1'b0; msg_type <= 8'd0; msg_payload <= {(8*MAX_PAYLOAD){1'b0}};
            crc_errors <= 16'd0; len_errors <= 16'd0; unknown_type <= 16'd0; rx_overflow <= 16'd0;
        end else begin
            msg_valid <= 1'b0;
            crc_feed <= 1'b0;
            crc_clear <= 1'b0;
            if (frame_end) begin
                // ---- end of frame: the §3 checks, in FrameDecoder order
                if (overflow) begin
                    // already counted when it happened
                end else if (code_rem != 8'd0) begin
                    len_errors <= len_errors + 1'b1;        // code runs past end
                end else if (dec_count < 9'd3) begin
                    // silent: link open / desync
                end else if (crc != {d1, d0}) begin
                    crc_errors <= crc_errors + 1'b1;
                end else if (tl[8]) begin
                    unknown_type <= unknown_type + 1'b1;
                end else if (dec_count - 9'd3 != tl) begin
                    len_errors <= len_errors + 1'b1;
                end else begin
                    msg_valid <= 1'b1;
                end
                enc_count <= 8'd0; overflow <= 1'b0; code_rem <= 8'd0; pending_zero <= 1'b0;
                dec_count <= 9'd0;
                crc_clear <= 1'b1;
            end else if (rx_valid && !overflow) begin
                if (enc_count == 8'd254) begin
                    overflow <= 1'b1;
                    rx_overflow <= rx_overflow + 1'b1;
                end else begin
                    enc_count <= enc_count + 1'b1;
                    if (code_rem == 8'd0) begin
                        // code byte; a finished block before it implies a zero
                        code_rem <= rx_data - 1'b1;
                        pending_zero <= 1'b1;
                        if (pending_zero) begin
                            d0 <= d1; d1 <= 8'd0;
                            crc_byte <= d0; crc_feed <= (dec_count >= 9'd2);
                            if (dec_count == 9'd0) msg_type <= 8'd0;
                            for (b = 1; b <= MAX_PAYLOAD; b = b + 1)
                                if (dec_count == b[8:0]) msg_payload[8*(b-1) +: 8] <= 8'd0;
                            dec_count <= dec_count + 1'b1;
                        end
                    end else begin
                        code_rem <= code_rem - 1'b1;
                        d0 <= d1; d1 <= rx_data;
                        crc_byte <= d0; crc_feed <= (dec_count >= 9'd2);
                        if (dec_count == 9'd0) msg_type <= rx_data;
                        for (b = 1; b <= MAX_PAYLOAD; b = b + 1)
                            if (dec_count == b[8:0]) msg_payload[8*(b-1) +: 8] <= rx_data;
                        dec_count <= dec_count + 1'b1;
                    end
                end
            end
        end
    end
endmodule
