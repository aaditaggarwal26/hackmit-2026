// uart_tx: 8N1 transmitter. `start` with `data` while !busy sends
// start bit, 8 data bits LSB first, stop bit. Idle line is high.

module uart_tx #(
    parameter CLKS_PER_BIT = 868
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output reg        txd,
    output wire       busy
);
    localparam CW = $clog2(CLKS_PER_BIT + 1);
    /* verilator lint_off WIDTHTRUNC */
    localparam [CW-1:0] LAST = CLKS_PER_BIT - 1;
    /* verilator lint_on WIDTHTRUNC */

    reg [CW-1:0] cnt;
    reg [3:0]    bit_idx;             // 0 = start, 1..8 = data, 9 = stop
    reg [9:0]    shreg;               // {stop, data[7:0], start}
    reg          active;
    assign busy = active;

    always @(posedge clk) begin
        if (!rst_n) begin
            active <= 1'b0;
            txd <= 1'b1;
            cnt <= {CW{1'b0}};
            bit_idx <= 4'd0;
            shreg <= 10'h3FF;
        end else if (!active) begin
            txd <= 1'b1;
            if (start) begin
                active <= 1'b1;
                shreg <= {1'b1, data, 1'b0};
                cnt <= {CW{1'b0}};
                bit_idx <= 4'd0;
            end
        end else begin
            txd <= shreg[0];
            if (cnt == LAST) begin
                cnt <= {CW{1'b0}};
                shreg <= {1'b1, shreg[9:1]};
                bit_idx <= bit_idx + 1'b1;
                if (bit_idx == 4'd9) active <= 1'b0;
            end else begin
                cnt <= cnt + 1'b1;
            end
        end
    end
endmodule
