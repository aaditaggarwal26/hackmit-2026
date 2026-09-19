// i2c_master: generic single-master I2C at ~100 kHz (CLKS_PER_QUARTER = a
// quarter of the SCL period: 250 clocks at 100 MHz). Commands, one at a
// time: START (also serves as repeated start), WRITE a byte (returns the
// slave's ACK), READ a byte (sends ACK or NACK as asked), STOP.
//
// Both lines are open-drain: *_oe = 1 pulls the line low, 0 releases it to
// the pull-up (top level: assign sda = sda_oe ? 1'b0 : 1'bz). sda_i is the
// synchronised line state. Clock stretching is not supported (the INA219
// does not stretch), so scl is never read back.
//
// Bit timing in quarters of the SCL period: q0 SCL low, SDA set; q1 SCL
// rises; q2 SCL high, SDA sampled; q3 SCL falls. START from idle or from
// the SCL-low state between bytes: release SDA (q0), SCL high (q1), SDA
// low while SCL high (q2), SCL low (q3). STOP: SDA low (q0), SCL high (q1),
// SDA released while SCL high (q2).

module i2c_master #(
    parameter CLKS_PER_QUARTER = 250
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       cmd_valid,
    input  wire [1:0] cmd,              // 0 START, 1 WRITE, 2 READ, 3 STOP
    input  wire [7:0] wdata,
    input  wire       read_nack,        // READ: 1 = NACK after the byte (last byte)
    output reg        busy,
    output reg        done,             // one-cycle pulse when the command finished
    output reg  [7:0] rdata,
    output reg        ack,              // WRITE: 1 = slave acknowledged; START: 1 = released SDA read high
    output reg        scl_oe,
    output reg        sda_oe,
    input  wire       sda_i
);
    localparam C_START = 2'd0, C_WRITE = 2'd1, C_READ = 2'd2, C_STOP = 2'd3;
    localparam QW = $clog2(CLKS_PER_QUARTER + 1);
    /* verilator lint_off WIDTHTRUNC */
    localparam [QW-1:0] QLAST = CLKS_PER_QUARTER - 1;
    /* verilator lint_on WIDTHTRUNC */

    reg [1:0]  sync;
    always @(posedge clk) sync <= {sync[0], sda_i};
    wire sda_in = sync[1];

    reg [QW-1:0] qcnt;
    reg [1:0]    q;                     // quarter within the current bit
    reg [3:0]    bit_idx;               // 0..7 data, 8 = ack bit
    reg [1:0]    cur;
    reg [7:0]    shreg;
    reg          nack_q;
    wire qtick = (qcnt == QLAST);

    always @(posedge clk) begin
        if (!rst_n) begin
            busy <= 1'b0; done <= 1'b0; rdata <= 8'd0; ack <= 1'b0;
            scl_oe <= 1'b0; sda_oe <= 1'b0;
            qcnt <= {QW{1'b0}}; q <= 2'd0; bit_idx <= 4'd0; cur <= C_START; shreg <= 8'd0; nack_q <= 1'b0;
        end else begin
            done <= 1'b0;
            if (!busy) begin
                qcnt <= {QW{1'b0}};
                q <= 2'd0;
                bit_idx <= 4'd0;
                if (cmd_valid) begin
                    busy <= 1'b1;
                    cur <= cmd;
                    shreg <= wdata;
                    nack_q <= read_nack;
                end
            end else if (!qtick) begin
                qcnt <= qcnt + 1'b1;
            end else begin
                qcnt <= {QW{1'b0}};
                q <= q + 2'd1;
                case (cur)
                    C_START: begin
                        case (q)
                            2'd0: sda_oe <= 1'b0;           // release SDA (SCL is low or idle-high)
                            2'd1: scl_oe <= 1'b0;           // SCL high
                            // A released SDA must read high: if it does not, the bus is
                            // unpowered, stuck, or simply not there (an undriven inout in a
                            // simulator reads 0), and every later "ACK" would be a lie.
                            2'd2: begin sda_oe <= 1'b1; ack <= sda_in; end   // SDA falls while SCL high = START
                            2'd3: begin scl_oe <= 1'b1; busy <= 1'b0; done <= 1'b1; end
                        endcase
                    end
                    C_STOP: begin
                        case (q)
                            2'd0: sda_oe <= 1'b1;           // SDA low with SCL low
                            2'd1: scl_oe <= 1'b0;           // SCL high
                            2'd2: sda_oe <= 1'b0;           // SDA rises while SCL high = STOP
                            2'd3: begin busy <= 1'b0; done <= 1'b1; end
                        endcase
                    end
                    C_WRITE: begin
                        case (q)
                            2'd0: sda_oe <= (bit_idx == 4'd8) ? 1'b0 : ~shreg[7];   // MSB first; release for the ACK bit
                            2'd1: scl_oe <= 1'b0;
                            2'd2: if (bit_idx == 4'd8) ack <= ~sda_in;              // slave pulls low to ACK
                            2'd3: begin
                                scl_oe <= 1'b1;
                                shreg <= {shreg[6:0], 1'b0};
                                bit_idx <= bit_idx + 1'b1;
                                if (bit_idx == 4'd8) begin sda_oe <= 1'b0; busy <= 1'b0; done <= 1'b1; end
                            end
                        endcase
                    end
                    C_READ: begin
                        case (q)
                            2'd0: sda_oe <= (bit_idx == 4'd8) ? ~nack_q : 1'b0;     // release for data; drive ACK/NACK bit
                            2'd1: scl_oe <= 1'b0;
                            2'd2: if (bit_idx != 4'd8) shreg <= {shreg[6:0], sda_in};
                            2'd3: begin
                                scl_oe <= 1'b1;
                                bit_idx <= bit_idx + 1'b1;
                                if (bit_idx == 4'd8) begin
                                    sda_oe <= 1'b0; rdata <= shreg; busy <= 1'b0; done <= 1'b1;
                                end
                            end
                        endcase
                    end
                endcase
            end
        end
    end
endmodule
