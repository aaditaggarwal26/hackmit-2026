// Phase-zero probe: smallest synthesizable module proving the simulator +
// cocotb loop works. One register, one adder. If this test fails nothing
// downstream is trustworthy. (Comment lines must not begin with the
// simulator's name — that spelling is parsed as a lint pragma.)
module hello #(
    parameter WIDTH = 8
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             en,
    output reg  [WIDTH-1:0] count
);
    always @(posedge clk) begin
        if (!rst_n)      count <= {WIDTH{1'b0}};
        else if (en)     count <= count + 1'b1;
    end
endmodule
