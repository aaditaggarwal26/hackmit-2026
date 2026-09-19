// frame_store: one FRAME_BYTES image as WORDS words of WORD_W bits (PIXELS_PER_CYCLE
// pixels per word, pixel j of a word in bits [8j +: 8]). Write one word per clock,
// registered read (data the cycle after rd_addr) so Vivado infers block RAM.
// Zero at power-on (protocol.md §5.1): the golden model scores against an all-zero
// reference until REF_FRAME_SET, and this must agree.

module frame_store #(
    parameter WORD_W = 64,
    parameter WORDS  = 2048,
    parameter ADDR_W = 11
) (
    input  wire              clk,
    input  wire              wr_en,
    input  wire [ADDR_W-1:0] wr_addr,
    input  wire [WORD_W-1:0] wr_data,
    input  wire [ADDR_W-1:0] rd_addr,
    output reg  [WORD_W-1:0] rd_data
);
    (* ram_style = "block" *) reg [WORD_W-1:0] mem [0:WORDS-1];
    integer i;
    initial for (i = 0; i < WORDS; i = i + 1) mem[i] = {WORD_W{1'b0}};

    always @(posedge clk) begin
        if (wr_en) mem[wr_addr] <= wr_data;
        rd_data <= mem[rd_addr];
    end
endmodule
