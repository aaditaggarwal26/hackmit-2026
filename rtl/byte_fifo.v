// byte_fifo: synchronous FIFO, 2**ADDR_W bytes, registered read (data is
// valid the cycle after rd_en) so Vivado infers block RAM for the storage
// (4096 x 8 = one RAMB36 at the default ADDR_W). `count` lets the TX framer
// refuse to start a frame that would not fit.

module byte_fifo #(
    parameter ADDR_W = 12
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        wr_en,
    input  wire [7:0]  wr_data,
    input  wire        rd_en,
    output reg  [7:0]  rd_data,
    output wire        empty,
    output wire        full,
    output wire [ADDR_W:0] count
);
    (* ram_style = "block" *) reg [7:0] mem [0:(1 << ADDR_W) - 1];
    reg [ADDR_W:0] wr_ptr, rd_ptr;      // one extra bit distinguishes full from empty

    assign count = wr_ptr - rd_ptr;
    assign empty = (wr_ptr == rd_ptr);
    assign full  = (count == (1 << ADDR_W));

    always @(posedge clk) begin
        if (wr_en && !full) mem[wr_ptr[ADDR_W-1:0]] <= wr_data;
    end

    always @(posedge clk) begin
        if (rd_en && !empty) rd_data <= mem[rd_ptr[ADDR_W-1:0]];
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            wr_ptr <= {(ADDR_W+1){1'b0}};
            rd_ptr <= {(ADDR_W+1){1'b0}};
        end else begin
            if (wr_en && !full)  wr_ptr <= wr_ptr + 1'b1;
            if (rd_en && !empty) rd_ptr <= rd_ptr + 1'b1;
        end
    end
endmodule
