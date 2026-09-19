// priority_queue: systolic shift-register queue, protocol.md §5.3. DEPTH cells of
// {valid, score, id}, head at cell 0, score descending, earlier insert first among
// equals. Every operation is one clock, all comparisons in parallel:
//   insert  cell i keeps itself if ge[i] (score[i] >= new), takes the new entry if
//           !ge[i] && (i == 0 || ge[i-1]), else takes cell i-1. ge is a prefix because
//           the cells are sorted, so exactly one cell takes the new entry. When full
//           (count == limit) the tail (cell limit-1) is lost if new > tail, otherwise
//           the newcomer is; `lost_id` says which, `evicted` counts either.
//   pop     every cell takes cell i+1; the last goes invalid.
//   set_limit cells at index >= limit go invalid; the dropped count joins `evicted`.
// Only one of insert / pop / set_limit per cycle (the controller sequences them).

`include "orbit_params.vh"

module priority_queue #(
    parameter DEPTH = `ORBIT_QUEUE_DEPTH,
    parameter CW    = 6                             // >= $clog2(DEPTH + 1)
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          insert,
    input  wire [15:0]   in_score,
    input  wire [15:0]   in_id,
    input  wire          pop,
    input  wire          set_limit,
    input  wire [CW-1:0] new_limit,                 // 1..DEPTH (validated by the controller)
    output reg  [CW-1:0] limit,
    output reg  [CW-1:0] count,
    output wire          has_data,
    output wire [15:0]   top_score,
    output wire [15:0]   top_id,
    output reg  [15:0]   evicted,                   // u16, wraps
    output reg  [15:0]   lost_id                    // after an insert: id lost, or 0xFFFF
);
    reg          valid [0:DEPTH-1];
    reg [15:0]   score [0:DEPTH-1];
    reg [15:0]   id    [0:DEPTH-1];

    assign has_data  = valid[0];
    assign top_score = valid[0] ? score[0] : 16'd0;
    assign top_id    = valid[0] ? id[0] : 16'hFFFF;

    // insert decisions (combinational, from the current state)
    wire full = (count == limit);
    wire [CW-1:0] tail_idx = limit - 1'b1;
    reg  [15:0] tail_score, tail_id;
    integer t;
    always @(*) begin
        tail_score = 16'd0; tail_id = 16'hFFFF;
        for (t = 0; t < DEPTH; t = t + 1)
            if (t[CW-1:0] == tail_idx) begin tail_score = score[t]; tail_id = id[t]; end
    end
    // set_limit: how many occupied cells fall beyond the new limit
    reg [CW:0] drop_cnt;
    integer q;
    always @(*) begin
        drop_cnt = {(CW+1){1'b0}};
        for (q = 0; q < DEPTH; q = q + 1)
            if (valid[q] && q[CW-1:0] >= new_limit) drop_cnt = drop_cnt + 1'b1;
    end
    wire newcomer_lost = full && (in_score <= tail_score);
    wire tail_lost     = full && (in_score > tail_score);

    reg ge [0:DEPTH-1];
    integer c;
    always @(*) begin
        for (c = 0; c < DEPTH; c = c + 1) ge[c] = valid[c] && (score[c] >= in_score);
    end

    integer n;
    always @(posedge clk) begin
        if (!rst_n) begin
            limit <= DEPTH[CW-1:0]; count <= {CW{1'b0}}; evicted <= 16'd0; lost_id <= 16'hFFFF;
            for (n = 0; n < DEPTH; n = n + 1) begin valid[n] <= 1'b0; score[n] <= 16'd0; id[n] <= 16'd0; end
        end else if (insert) begin
            if (newcomer_lost) begin
                lost_id <= in_id;
                evicted <= evicted + 1'b1;
            end else begin
                lost_id <= tail_lost ? tail_id : 16'hFFFF;
                if (tail_lost) evicted <= evicted + 1'b1;
                else count <= count + 1'b1;
                for (n = 0; n < DEPTH; n = n + 1) begin
                    if (n[CW-1:0] >= limit) begin
                        valid[n] <= 1'b0;                        // never occupied beyond the limit
                    end else if (ge[n]) begin
                        // keep
                    end else if (n == 0 || ge[n == 0 ? 0 : n - 1]) begin
                        valid[n] <= 1'b1; score[n] <= in_score; id[n] <= in_id;
                    end else begin
                        valid[n] <= valid[n == 0 ? 0 : n - 1]; score[n] <= score[n == 0 ? 0 : n - 1]; id[n] <= id[n == 0 ? 0 : n - 1];
                    end
                end
            end
        end else if (pop) begin
            if (valid[0]) count <= count - 1'b1;
            for (n = 0; n < DEPTH - 1; n = n + 1) begin
                valid[n] <= valid[n + 1]; score[n] <= score[n + 1]; id[n] <= id[n + 1];
            end
            valid[DEPTH - 1] <= 1'b0;
        end else if (set_limit) begin
            limit <= new_limit;
            for (n = 0; n < DEPTH; n = n + 1)
                if (n[CW-1:0] >= new_limit) valid[n] <= 1'b0;
            count <= (count > new_limit) ? new_limit : count;
            evicted <= evicted + {{(16-CW-1){1'b0}}, drop_cnt};
        end
    end
endmodule
